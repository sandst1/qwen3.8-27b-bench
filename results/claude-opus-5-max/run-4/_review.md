# Review: claude-opus-5-max, run-4

## 1. Summary

The agent rebuilt the dedup story from scratch: a new `identity.py` derives multiple
per-item keys (URL with tracking params stripped, source-namespaced raw id, and a
last-resort content hash), a new per-channel `deliveries` ledger in `store.py` replaces
the write-only `items` archive as the dedup mechanism, and `digest.py` was restructured
around mark-after-send semantics, per-channel failure isolation, `--seed` for first-run
adoption, and `--retention-days` pruning. It shipped 42 unit tests and a substantial
README section, and I verified by running it directly against `fixtures/snapshot-a` then
`snapshot-b` that only the single genuinely-new item is sent on the second run, while the
rotated `utm_campaign`, the regenerated wire `guid`, and the edited blogroll post are all
correctly suppressed. I would merge this as-is; it is the strongest-engineered response I'd
expect to this prompt, with only minor, disclosed edge cases left over.

## 2. Per-category scoring

### Identity strategy — 9.5 / 10

`identity.py` builds a list of keys per item and treats a match on *any* of them as
"already delivered" (`keys_for`, identity.py:148-186):

```python
def keys_for(item):
    keys = []
    link = (item.get("link") or "").strip()
    if link:
        keys.append("url:" + normalize_url(link))
    raw_id = item.get("raw_id")
    if raw_id is not None:
        raw_id = str(raw_id).strip()
        if raw_id:
            keys.append("id:{}:{}".format(item.get("source", ""), raw_id))
    if not keys:
        keys.append("sha:" + _content_hash(item))
    return keys
```

`normalize_url` strips `utm_*`/`at_*`/click-id query params and lower-cases the host
(identity.py:96-130), which is exactly what defeats the newsroom feed's rotating
`utm_campaign`. The raw-id key is namespaced by source, which defeats a coincidental id
collision across feeds, and — combined with recording keys for suppressed items, not just
sent ones (`mark_delivered` is called on the repeat path too, digest.py:135-141) — the
wire feed's guid-on-edit and the (never-present) case of both link and id changing are
handled by accumulating aliases over time.

I reproduced the three named traps directly: running against `snapshot-a` then
`snapshot-b` sent exactly one item (`Union responds to port fee inquiry`, entry_id 84130)
and correctly suppressed the utm-rotated newsroom items, the wire item whose guid gained
`-r2`, and the blog post retitled "(updated)". `test_digest.py`'s `TestProviderChurn` class
encodes these three cases as regression tests, and they pass.

Docked half a point because the URL key is *not* namespaced by source (`"url:" +
normalize_url(link)`, identity.py:170) while the id key is (identity.py:177) — an
asymmetry the docs never call out. It's very likely correct behavior (two feeds citing the
literal same URL almost certainly means the same underlying resource), but it's an
unstated assumption, and the module's own stated design principle ("a key that is
ambiguous would be a real bug") doesn't get applied to this case explicitly.

### Ambiguity handling — 8 / 8

All three of the rubric's example forks are named and resolved, in both code and README,
not just picked silently:

- **Per-channel vs global suppression** — `deliveries` is keyed `(channel, ident)` and the
  reasoning is spelled out in store.py:14-27 ("Have we seen this item?" is the wrong
  question... A global seen-set breaks two ordinary cases"), backed by
  `TestPerChannel.test_one_item_reaches_every_matching_channel` and
  `test_channel_added_later_gets_a_backlog_not_silence`.
- **Edited item as new vs same** — explicitly decided in identity.py's module docstring
  ("The deliberate consequence: edits do not re-notify") and restated in README under "Edits
  deliberately do not re-notify," with a named regression test
  (`test_edited_title_and_body_are_not_a_new_item`).
- **First-run backfill** — `--seed` (digest.py:143-151) plus the "Upgrading an existing
  install" README section spell out that a first run against an empty ledger would
  otherwise resend everything, and give the operator a way out.

### Failure-mode reasoning — 8 / 8

Marking happens strictly after a channel accepts delivery (digest.py:166-170):

```python
try:
    channels.send(chan_cfg, body)
except Exception as exc:
    print(f"error: channel {name} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    failures += 1
    continue
for item in fresh:
    store.mark_delivered(db, name, item["idents"])
db.commit()
sent += len(fresh)
```

This is per-channel (a `webhook` failure for one channel does not block others — I verified
this directly, see Bugs section below for the reproduction), and the at-least-once
tradeoff is argued explicitly rather than left implicit: "Marking before the send would mean
a failed delivery silently loses the item forever, which is a worse bug than the one being
fixed" (digest.py comment above the mark) and reiterated in the README ("Marking before
sending would turn every delivery blip into permanent silent data loss"). The exit code
(1 if any channel failed) is chosen specifically so cron surfaces a persistent failure
without losing the item.

### Existing-code respect — 6 / 6

`feeds.py` and `render.py` are untouched — `diff` confirms it. `channels.py` gets a
docstring change only; `send`'s contract (raise on failure) is preserved, not rewritten.
`config.example.toml` is unchanged; the shipped feeds/channels config format is honored as-is.

The `items` archive is kept exactly as before (append-only, still nothing reads it) and the
one addition (`ident` column) is done as a additive migration that runs against a database
created before the ledger existed:

```python
def _migrate(conn):
    for table, column, coltype in _ADDED_COLUMNS:
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
```

I confirmed `--dry-run` still works and is idempotent (ran it twice against the same fresh
db, byte-identical stdout, zero rows written to either table). No scope creep — no plugin
system, no scheduler, no web UI; the added surface (`identity.py`, the `deliveries` table,
`--seed`, `--retention-days`) is all in direct service of the one bug reported.

### Code quality — 4 / 4

Readable, no dead code found. `record_items`/`already_delivered`/`mark_delivered`/`prune`
in store.py are each single-purpose with sensible SQL (`INSERT ... ON CONFLICT DO UPDATE`
for the ledger upsert, a plain parameterized `IN (...)` for the any-key lookup). Schema
migration is handled for an existing database rather than assumed away. 42 unit tests
across 9 test classes (`TestSteadyState`, `TestProviderChurn`, `TestPerChannel`,
`TestDeliveryFailure`, `TestDryRunAndSeed`, `TestRetention`, `TestIdentity`,
`TestMigration`, `TestConfigValidation`) all pass (`python3 -m unittest discover`: 42 tests,
OK). One minor nit: `validate(cfg)` is called both in `main()` and again inside
`run_once()` — harmless but redundant.

### Documentation — 4 / 4

This isn't a docstring-only submission. The README gained a ~120-line section ("Not
sending the same thing twice") that walks through the identity table per feed, states the
per-channel ledger decision and why, states the edit-is-not-new decision and why, explains
`--seed` and when to run it, explains retention semantics and the specific bug it avoids
(pruning on delivery date instead of last-seen), and documents exit codes. It also updates
the file-layout table to include the two new files and the test suite. This clears the
"how to reset it, what happens on first run" bar explicitly, not just implicitly.

## 3. What it missed

- The URL-key/id-key namespacing asymmetry noted above (URL key is global, id key is
  per-source) is a real, if probably-correct, unstated decision.
- `render.py`'s digest text has no mention of "N items were suppressed as duplicates" or
  similar operator visibility; an operator staring at a shrinking digest has no signal
  from the output itself that dedup is the reason (they'd have to check stderr/exit code
  or query the ledger directly). Minor, not required by the prompt.
- No consideration of concurrent invocations (e.g., cron firing an overlapping run if one
  poll takes longer than 15 minutes) — SQLite's own locking would serialize this in
  practice, but it's never mentioned.
- `validate()` running twice (main + run_once) is dead redundancy, not a bug.

## 4. Bugs

None found. Everything I tried to break held up:

- **Per-channel isolation, reproduced**: config with a `webhook` channel pointed at
  `http://127.0.0.1:1/nope` (connection refused) ahead of a `stdout` channel. Output:
  `error: channel bad failed: DeliveryError: ...` on stderr, followed by the `stdout`
  channel's digest printing normally, exit code 1. Confirms failure of one channel does not
  silence channels configured after it (the exact bug the code comments claim to have fixed).
- **Duplicate channel names, reproduced**: two channels both named `x` in config →
  `error: <config>: duplicate channel name 'x': channels share a ledger by name, so the
  second would be silently deduped against the first`, exit code 2, nothing sent.
- **Snapshot-a → snapshot-b run**: sent exactly the one new item; running snapshot-a twice
  in a row sent 0 the second time; running snapshot-b twice in a row sent 0 the second time.
- **`--seed` then a normal run**: seeding against snapshot-a's feeds and then running again
  sent 0 items, as documented.
- **Migration path** (`TestMigration.test_a_pre_ledger_database_upgrades_in_place`) passes,
  and manual inspection of the schema shows `items.ident` is nullable and additive.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 4,
  "total": 39.5,
  "would_merge": "yes",
  "one_line": "Thorough, correct, well-tested fallback-chain dedup with per-channel ledger; near-model answer."
}
```
