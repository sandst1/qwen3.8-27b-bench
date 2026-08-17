## 1. Summary

The agent added a per-feed `identity` field in `feeds.py` (choosing the field that is actually stable for each of the three formats), a `sent(identity, channel)` delivery ledger in `store.py` that's written only after a channel accepts a digest, and a schema migration that backfills `identity` for pre-existing databases. It documented all of this in the module docstrings and in a new README section. I would merge this: the identity choices are correct and verified against both fixture snapshots, the failure-mode story is sound and stated, and the diff is minimal and respectful of the existing code — modulo one real bug in the migration's legacy-identity backfill, described below.

## 2. Per-category scoring

### Identity strategy — 9/10

Per-feed, field-specific identity, explicitly reasoned about in `feeds.py:12-25`:

```python
- newsroom: entry_id is stable, so identity is the entry_id. (The URLs are
  not usable: they carry a ?utm_campaign= value that changes every poll.)
- blogroll: there is no identifier at all, and titles/excerpts change when
  a post is edited, so identity is the permalink — the only stable field.
- generic (wire): the guid is regenerated whenever an item is edited
  (typos, retitles), so identity is the link, not the guid.

Identity is namespaced with the feed name so two feeds carrying the same
URL can never collide.
```

and implemented per-branch in `fetch()`:

```python
"identity": f"{name}:{r['entry_id']}",   # newsroom
"identity": f"{name}:{r['permalink']}",  # blogroll
"identity": f"{name}:{r['link']}",       # generic/wire
```

I ran it against both fixtures to check this isn't just asserted. `snapshot-a` then `snapshot-b`:

```
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
```

Only the genuinely new newsroom entry (`entry_id 84130`) was resent. The rotated `utm_campaign` (w33→w34) on two unchanged newsroom entries did not cause a re-send, the blogroll post that was edited in place (title/excerpt changed, permalink same) did not re-send, and the wire item whose `guid` was regenerated (`wire-...-0031` → `wire-...-0031-r2`, same link) did not re-send either. That is precisely the three failure modes the rubric calls out, all handled correctly on both fixture snapshots.

Docked half a point because the migration path (see Code Quality below) reintroduces a guid-trusting identity for legacy rows, which is inconsistent with this stated strategy — a latent trap for whoever runs the migration on a real `wire` table with historical rows.

### Ambiguity handling — 8/8

All three canonical forks are named and resolved, not just implemented silently:

- **Per-channel vs global suppression** — resolved explicitly as per-channel. `store.py:8-12`: *"`sent` — the delivery ledger: one row per (item, channel) pair that has been delivered successfully."* Schema: `PRIMARY KEY (identity, channel)` (`store.py:41`). `digest.py:53` fetches `already = store.sent_identities(db, chan_cfg["name"])` per channel inside the channel loop, so channel A having received an item never suppresses it for channel B — verified above, `ops` and `everything` both showed the same items on the first run.
- **Edited item as new vs same** — resolved as *same*, and the reasoning is given per-feed (quoted above): identity intentionally ignores the field that changes on edit (guid) in favor of the one that doesn't (link), and for blogroll uses permalink because title/excerpt are exactly what an edit touches. README also states the flip side: *"If an item's identity changes upstream (e.g. a permalink moves), it is treated as a new item and sent again."* (README.md:53-54).
- **First-run backfill** — stated plainly in README.md:50-51: *"The first run against a fresh (or deleted) db sends everything currently in the feeds, once. Don't delete `digest.sqlite3` casually."* — verified: first run against an empty db sent 12 items, second run against the same snapshot sent 0.

### Failure-mode reasoning — 8/8

Per-channel marking, correct ordering, and the guarantee is argued rather than assumed. `digest.py:60-70`:

```python
channels.send(chan_cfg, body)
# Mark only after a successful send: if the channel fails, the
# exception propagates (by design, see channels.py) and the next
# tick retries exactly the channels that did not get it.
store.mark_sent(db, chan_cfg["name"], [i["identity"] for i in selected])
sent += len(selected)
```

`mark_sent` is only reached if `channels.send` didn't raise, so a channel outage produces at-least-once delivery for that channel and does not touch other channels' ledger rows (per-channel `PRIMARY KEY`). The tradeoff is named directly in README.md:43-46: *"A `sent` row is written only after the channel accepts the digest. If a webhook is down, the exception propagates ... and the next tick retries just the channels that missed it — channels that already got the item are not re-sent."* This matches the existing `channels.py` contract (`"Delivery is best effort: if a channel is down we let the exception propagate"`), so the agent built on the codebase's existing crash semantics instead of inventing new ones.

One caveat not mentioned: because `run_once`'s channel loop has no per-channel `try/except`, a channel raising also aborts processing of *subsequent* channels in the same tick (they simply get retried on the next tick 15 minutes later, with no data loss — but this is worth knowing and isn't discussed).

### Existing-code respect — 5.5/6

The diff is surgical: `render.py` and `channels.py` are untouched, `digest.py`'s control flow is the same shape with two added lines (ledger lookup + mark_sent), and `--dry-run` still works and still does not write to the ledger (verified: two consecutive `--dry-run` invocations against the same fixtures produced byte-identical output and left the `sent` table at 0 rows). The pre-existing `items` archive is kept for its stated purpose and the unconditional insert is fixed via `INSERT OR IGNORE` plus a real schema migration (`store.py:59-88`) rather than a "just delete the db" instruction — this is exactly what the rubric asks for.

Half a point off: the migration's legacy-identity backfill is subtly wrong (see next section), which is a code-quality issue that also touches this category since it doesn't cleanly reconcile with the pre-existing schema it's migrating.

### Code quality — 3/4

Clean, no dead code, no vestigial columns — `raw_id` is deliberately retained for the archive and is explicitly justified ("kept verbatim for the archive"). SQL is straightforward and correct for the steady-state case. The migration is a genuine, tested schema migration, not a "reset the db" cop-out — a legitimate strength given the rubric explicitly calls out "schema migration handled for an existing database."

However, the migration has a real bug. `store.py:66-76`:

```python
cols = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
if "identity" not in cols:
    # Pre-identity database. Backfill the way feeds.py would have
    # derived identity: the provider's id when that one was stable,
    # otherwise the link. Legacy `generic` rows were keyed by guid,
    # which we no longer trust, so those items are re-archived (and
    # re-sent exactly once) under their link-based identity.
    conn.execute("ALTER TABLE items ADD COLUMN identity TEXT")
    conn.execute(
        "UPDATE items SET identity = source || ':' || COALESCE(raw_id, link)"
    )
```

The comment says legacy `generic`/wire rows should get a *link*-based identity because their old guid is untrusted — but the code does `COALESCE(raw_id, link)`, which picks `raw_id` (the guid) whenever it's non-null, which for wire rows it always is. I reproduced this: I built a legacy items table (no `identity` column, one wire row with `raw_id='wire-2026-08-14-0031'`), ran `store.connect()` on it to trigger the migration, then ran a normal `digest.py` tick against `snapshot-a`. Result — two archive rows for the same real-world item:

```
{'id': 1, 'raw_id': 'wire-2026-08-14-0031', 'link': '.../i/0031', 'identity': 'wire:wire-2026-08-14-0031'}   # migrated row, guid-based
{'id': 7, 'raw_id': 'wire-2026-08-14-0031', 'link': '.../i/0031', 'identity': 'wire:https://wire.example/i/0031'}  # freshly-fetched row, link-based
```

This does not cause a duplicate *notification* — the `sent` table starts empty regardless of migration, so delivery correctness is unaffected in this specific scenario — but it does break the archive's own "one row per identity" invariant the migration was trying to establish, and it contradicts the migration's own comment. It's a real, demonstrable bug, just a contained one.

### Documentation — 4/4

Beyond a docstring: the README gets a new, substantive "Dedup: why you don't get the same item twice" section (README.md:31-56) covering the identity rationale, the sent-ledger mechanism, the ordering guarantee, first-run behavior, `--dry-run` interaction, identity-change-means-resend, and unbounded table growth. This is exactly the rubric's ask (dedupe behaviour, reset, first run) and then some.

## 3. What it missed

- **Mid-run partial failure across channels**: if channel 2 of 3 raises, channels 3+ in that same config never get attempted this tick (the loop has no per-channel try/except). Not incorrect — the item is just delayed one tick, no loss or duplication — but it's an undiscussed corner of the "per-channel" story the agent otherwise argues carefully.
- **No reset/backfill tooling**: README says "don't delete `digest.sqlite3` casually" but there's no documented way to force a re-send for one channel (e.g., a new channel added later, which will legitimately want the full current backlog) versus resetting everything. Minor, but the README raises the first-run behavior without saying how to deliberately re-trigger it for a single channel.
- **Unbounded table growth** is *named* (README:55-56, "prune ... if that ever becomes a problem") but not addressed — reasonable to punt, and explicitly flagged as a punt, which is the right way to leave it.
- **The migration COALESCE bug** above was not caught by the agent itself; its own docstring describes the intended behavior correctly but the implementation doesn't match it.

## 4. Bugs

**Migration backfill uses guid instead of link for legacy generic/wire rows, contradicting its own comment.**

Reproduction:
```python
import sqlite3, store
conn = sqlite3.connect("test.sqlite3")
conn.execute("""CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL, raw_id TEXT, title TEXT NOT NULL, link TEXT NOT NULL,
  summary TEXT, published TEXT, first_seen TEXT NOT NULL DEFAULT (datetime('now')))""")
conn.execute("INSERT INTO items (source, raw_id, title, link) VALUES "
             "('wire','wire-2026-08-14-0031','x','https://wire.example/i/0031')")
conn.commit(); conn.close()

db = store.connect("test.sqlite3")   # runs _migrate()
# row's identity is now 'wire:wire-2026-08-14-0031' (guid-based) —
# but feeds.py computes 'wire:https://wire.example/i/0031' (link-based)
# for the same real item on the next fetch, producing a duplicate archive row.
```
Impact is contained to the `items` archive (not the `sent` delivery ledger, which always starts empty on migration, so no double notification results from this specific bug) — but it is a genuine, verifiable defect in code whose whole job is schema correctness.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 5.5,
  "code_quality": 3,
  "documentation": 4,
  "total": 37.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Correct dedupe and ledger design; one real bug in legacy-migration identity backfill."
}
```
