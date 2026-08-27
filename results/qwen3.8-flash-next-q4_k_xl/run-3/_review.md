# Review — notify-digest dedupe fix

## 1. Summary

The agent replaced the "send everything every tick" behaviour with a per-item `uid` (chosen per feed via a new `dedupe_by` config key) and a per-channel `deliveries` ledger in SQLite, written only after a channel's send succeeds; it also wrote a migration for existing databases and a substantial README section explaining the choices. I verified against both fixture snapshots that a re-run sends nothing, that moving from snapshot-a to snapshot-b sends only the genuinely new item, and that `--dry-run` never touches the ledger. I would merge this with fixes: the core design is sound and well-argued, but the schema migration has a real (if low-impact) bug, and the README omits two of the three things the rubric asks it to cover (how to reset the ledger, what a first run does).

## 2. Per-category scoring

### Identity strategy — 10 / 10

The agent picked a **fallback-chain identity** driven by a new per-feed `dedupe_by` config key, stated explicitly with each feed's failure mode:

```python
# feeds.py:109-113
def _attach_uids(items, feed_cfg):
    by = feed_cfg.get("dedupe_by", "link")
    for item in items:
        key = item["raw_id"] if by == "raw_id" and item["raw_id"] else item["link"]
        item["uid"] = f"{item['source']}|{key}"
```

with the config wiring it per feed:

```toml
# config.example.toml
[[feeds]]
name = "newsroom"
...
dedupe_by = "raw_id"   # entry_id is stable; the URL's utm_campaign rotates weekly.

[[feeds]]
name = "wire"
...
dedupe_by = "link"     # The guid is regenerated whenever an item is edited; the link is stable.
```

This is exactly what the fixtures are designed to test, and I ran both against `snapshot-a` then `snapshot-b`:

- Newsroom: `entry_id` 84121 stays constant while the URL's `utm_campaign` rotates `w33`→`w34` — not re-sent. ✅
- Wire: guid goes from `wire-2026-08-14-0031` to `wire-2026-08-14-0031-r2` on edit, link stays constant — not re-sent. ✅
- Blogroll: no `raw_id` at all, title changes to "(updated)", permalink stays constant — not re-sent, correctly falls back to `link` via the `and item["raw_id"]` guard. ✅
- The only genuinely new item (`newsroom` entry 84130) *is* sent on the second run.

The docstring in `feeds.py:12-30` states the choice and its accepted failure mode ("an edited item... is NOT re-sent... missing an edit is better than re-notifying everyone") up front. This is the top band of the rubric almost verbatim: per-feed identity, survives all three fixture traps, stated with its failure mode.

### Ambiguity handling — 6 / 8

Two of the three named forks are resolved *and* explicitly argued in prose:

- **Per-channel vs. global suppression** — resolved per-channel, and said so: `store.py:9-14` "`deliveries` is the dedup ledger: one row per (channel, uid)... It is per channel, so an item matching two channels is still delivered to both." I verified this directly — an item matching both `ops` and `everything` in the fixtures is delivered to both channels independently.
- **Edited item as new vs. same** — resolved as "same", with the tradeoff spelled out: `feeds.py:28-30`, and again in the README ("**Edits do not re-trigger a send.**... If you ever want update notifications, add a content hash to the ledger as a *separate* signal — do not weaken the uid.").

The third fork, **first-run backfill** (what happens the very first time the script runs against an empty/new database — send the entire current feed contents, or treat pre-existing items as already-seen), is never named. It is handled — correctly, in the sense that it does what a reasonable person would want (I confirmed a fresh `--db` sends everything currently in the feeds on run 1) — but there's no discussion of the alternative (e.g. suppressing an initial backlog burst when a new channel is added to config), so it's the "decided silently but correctly" case the rubric describes for the mid band, not the top band which requires all three named.

### Failure-mode reasoning — 7.5 / 8

Per-channel marking, correct ordering, and the tradeoff is argued in-line:

```python
# digest.py:61-64
channels.send(chan_cfg, body)
# Mark only after a successful send: if delivery fails the
# exception propagates and the items go out on the next tick.
store.mark_delivered(db, chan_cfg["name"], [i["uid"] for i in selected])
```

I reproduced the "channel two fails after channel one succeeded" case directly (three channels, the middle one pointed at a closed port):

```
=== ops ===                              # channel 1, stdout, succeeds
...
EXCEPTION: energy: <urlopen error [Errno 61] Connection refused>   # channel 2
ops delivered: {'blogroll|...', 'newsroom|84121', 'wire|...'}      # channel 1 marked
energy delivered: set()                                            # channel 2 not marked — will retry
everything delivered: set()                                        # channel 3 never even attempted this tick
```

Channel 1's items are correctly marked and won't repeat; channel 2's items correctly stay unmarked and retry next tick (at-least-once, and the choice is stated in the comment above and in the README: "Delivery is marked only after the channel send succeeds, so a failing webhook retries on the next tick instead of silently dropping items."). This is the top band's description almost exactly.

The half-point deduction: channel 3 (`everything`, after the failing channel in config order) is silently skipped for that tick because the unhandled exception propagates out of `run_once` entirely — it isn't attempted until the *next* cron tick, 15 minutes later. That's a pre-existing property of the loop (not introduced by this change) and it doesn't lose or duplicate anything, but the rubric specifically asks "what happens when channel two fails after channel one succeeded", and the answer given only covers the failing channel itself, not the delay imposed on channels that haven't run yet.

### Existing-code respect — 6 / 6

The diff is minimal and works with the grain of the codebase rather than against it:

- `feeds.py`'s three format branches are untouched; the agent factored the existing `fetch()` body out into `_normalize()` and added a small `_attach_uids()` step, rather than rewriting the module (`feeds.py:59-113`).
- `channels.py` and `render.py` are untouched entirely (`diff` is empty).
- `store.py` keeps the `items` archive table and its stated purpose, and explicitly deals with the "unconditional insert" the rubric calls out: `INSERT OR IGNORE ... uid ...` against a new `UNIQUE INDEX idx_items_uid` (`store.py:76-77`, schema at `store.py:35`).
- `--dry-run` is not broken — I confirmed twice in a row that dry-run reports the same 6 items both times and never writes to `deliveries`:
  ```
  === second dry run should show same items again ===
  Ops digest — 3 item(s)
  Energy digest — 3 item(s)
  Firehose — 6 item(s)
  ```
- No scope creep — no plugin system, scheduler, or web UI was added.

### Code quality — 3 / 4

The new code (`_attach_uids`, `delivered_uids`, `mark_delivered`) is short, readable, and the SQL is sensible (composite primary key on `deliveries(channel, uid)`, `INSERT OR IGNORE` used correctly in both new write paths). The deduction is for the schema migration, which has a real inconsistency — see Bugs below: it backfills every old row's `uid` from `link` regardless of what `dedupe_by` that feed is configured with now, so on the first run after upgrading an old database, feeds configured with `dedupe_by = "raw_id"` (newsroom) get a second, differently-keyed archive row for items they'd already seen. It's caught by the `items` table's own logic (`INSERT OR IGNORE`) only going forward, not for that one boundary run, and the migration's own docstring (`store.py:53-61`) argues about the *wire* guid case but doesn't notice this newsroom case — i.e. the reasoning given for the migration doesn't actually cover the failure mode it has.

### Documentation — 3 / 4

The README gained a genuine, structured section (`README.md`, "How duplicates are prevented") rather than just a docstring: it explains the `uid` mechanism, gives a table of stable/unstable fields per feed matching the fixtures, states the edited-item tradeoff, states the per-channel choice, and states the dry-run/ledger interaction. That clears the "docstring alone caps this at 2" floor comfortably.

It falls short of 4/4 because the rubric asks specifically for three things and the README covers one thoroughly: it does **not** say how to reset the dedupe state (e.g. that deleting/truncating the `deliveries` table — or the whole `--db` file — will cause a resend of everything currently in the feeds), and it does not explicitly state what a first run against a fresh database does (it can be inferred, but isn't said, and "explains... what happens on first run" is literally the rubric's wording).

## 3. What it missed

- **First-run backlog burst is not named as a decision.** If someone adds a new channel to an existing, long-running deployment, that channel's first run will dump every currently-matching item across all feeds at once — potentially the entire fixture set. The agent never discusses whether that's desired or how to avoid it (e.g. pre-seeding `deliveries` for a new channel).
- **No reset/runbook instructions.** There's no documented way to force a resend (delete the DB, truncate `deliveries`, etc.) despite the README otherwise reading like an ops-facing document.
- **The migration doesn't reuse the feed's own identity logic** (see Bugs) — it re-derives a uid from `link` unconditionally instead of calling into `feeds._attach_uids`-equivalent logic, so it is inconsistent with the rest of the codebase's stated identity strategy for exactly the feed (`newsroom`) that the whole fix was designed around.
- **The channel-ordering/cascade-delay effect** (channel 3 not attempted this tick if channel 2 raises) is unchanged from the original code and not discussed, even though the new failure-mode comments discuss the adjacent case.
- No automated tests were added (none existed before either, so this isn't a regression, but nothing guards the migration path or the per-feed dedupe logic going forward).

## 4. Bugs

**Migration produces a duplicate, differently-keyed archive row for `raw_id`-deduped feeds on the first run after upgrading an old database.**

Reproduction:

```sh
# 1. Build an "old" database using the ORIGINAL (unpatched) code — two runs,
#    which is exactly the bug this task was filed about: every run re-archives
#    every item.
cd <pristine copy>
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
python3 digest.py --config config.toml --db digest.sqlite3
#   -> `items` table now has 12 rows (6 items x 2 runs), no `uid` column.

# 2. Point the AGENT's code at that same file.
cd <agent's tree>
cp digest.sqlite3 <path-from-step-1>
python3 digest.py --config config.toml --db digest.sqlite3

# 3. Inspect the archive:
python3 -c "
import sqlite3
c = sqlite3.connect('digest.sqlite3'); c.row_factory = sqlite3.Row
print(c.execute('SELECT COUNT(*) FROM items').fetchone()[0])
for r in c.execute('SELECT uid FROM items'): print(dict(r))
"
```

Observed output: 8 rows, not 6. The `newsroom` items each appear twice:

```
{'uid': 'newsroom|84118'}
{'uid': 'newsroom|84121'}
{'uid': 'newsroom|https://newsroom.example/2026/08/offshore-tender?utm_source=feed&utm_campaign=w33'}
{'uid': 'newsroom|https://newsroom.example/2026/08/port-fees?utm_source=feed&utm_campaign=w33'}
```

Cause: `_migrate()` backfills every pre-existing row's `uid` as `source || '|' || link` (`store.py:66`) regardless of the feed's configured `dedupe_by`. On the very next `fetch()`, the same newsroom items get a *fresh* `uid` computed from `raw_id` (`feeds.py:112`, `dedupe_by = "raw_id"` in `config.example.toml`), which doesn't match the migrated link-based uid, so `INSERT OR IGNORE` treats it as a new row instead of a duplicate.

**Impact:** this is *not* the duplicate-notification bug the task was filed about — the `deliveries` ledger is a brand-new table on any pre-existing database, so migrated installs already get one "everything looks new" burst as their first run under the new code regardless (that part is unavoidable and arguably fine, see "What it missed"). I confirmed a second run against the same (migrated) database sends 0 items, so the actual notification behaviour is unaffected. The bug is confined to the `items` archive table getting one extra, permanently-orphaned row per previously-seen `raw_id`-deduped item — cosmetic, since (per the module's own docstring) "nothing reads it at the moment" — but it's the kind of latent inconsistency that will confuse the "next person to touch it" the prompt asked the agent to think about, and it contradicts the migration docstring's own claimed worst case ("a handful of items get sent one last time" — that reasoning was about wire's rotating guid, and doesn't apply to, or account for, the newsroom case actually triggered).

```json
{
  "identity_strategy": 10,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 7.5,
  "existing_code_respect": 6,
  "code_quality": 3,
  "documentation": 3,
  "total": 35.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid per-feed identity + per-channel ledger; migration has a latent archive-dedup bug."
}
```
