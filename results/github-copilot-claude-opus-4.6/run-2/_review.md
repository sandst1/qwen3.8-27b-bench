# Review: notify-digest dedupe fix

## 1. Summary

The agent added a `sent_items` table keyed on `(item_id, channel)` and a `UNIQUE(source, raw_id)` constraint on `items`, then rewired `digest.py` to fetch-and-persist first and select only unsent rows per channel before marking them sent. The core loop logic is sound and per-channel suppression with post-send marking is the right shape for at-least-once delivery — but the schema change is applied via `CREATE TABLE IF NOT EXISTS`, which is a no-op against the database this job has presumably been writing to every 15 minutes for a while, so the fix does not activate on the one database that matters; separately, the `raw_id TEXT NOT NULL` constraint silently and permanently drops every item from the `blogroll` feed (which the code's own comment says has no stable id), with no warning printed anywhere. I would not merge this without the migration and the nullable-`raw_id` bug fixed first — as shipped, it plausibly fixes nothing in production while looking fixed in a demo run against a fresh `.sqlite3` file.

## 2. Per-category scoring

### Identity strategy — 5 / 10

The agent uses per-feed `raw_id` as the identity key, unified behind `UNIQUE(source, raw_id)`:

```sql
-- store.py
CREATE TABLE IF NOT EXISTS items (
    ...
    raw_id      TEXT NOT NULL,
    ...
    UNIQUE(source, raw_id)
);
```

This correctly survives the `newsroom` feed's rotating `utm_campaign` (verified below — `entry_id` 84121/84118 do not resend between snapshot-a and snapshot-b even though their `utm_campaign` changes from `w33` to `w34`), and it correctly treats `wire`'s regenerated `guid` on edit as a new item (an arguable, if unstated, choice for "edited item").

But `feeds.py` says explicitly, for the `blogroll` format:

```python
# feeds.py
if fmt == "blogroll":
    # No stable identifier of any kind in this one.
    return [
        {..., "raw_id": None}
        for r in doc.get("posts", [])
    ]
```

Making `raw_id NOT NULL` in the schema means every `blogroll` item fails the insert. Because `record_items` uses `INSERT OR IGNORE`, this failure is completely silent — no exception, no stderr warning, no row written, ever:

```python
# store.py
def record_items(conn, source, items):
    """Insert items we haven't seen before (duplicates are silently ignored)."""
    conn.executemany(
        "INSERT OR IGNORE INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ...
    )
```

Reproduced against a fresh database (`fixtures/snapshot-a`, `config.example.toml` which includes a `blogroll` feed and an `everything` channel with `keywords = []`):

```
$ python3 digest.py --config config.example.toml --db digest.sqlite3
=== everything ===
Firehose — 4 item(s)
...  # only newsroom + wire items; the two blogroll posts never appear
$ sqlite3 digest.sqlite3 'select source from items'
newsroom
newsroom
wire
wire
```

The two `blogroll` posts are gone from the archive and from every channel, permanently, on every run — worse than the duplicate-spam bug the task was about, since now that feed is silently muted instead of merely noisy. The other two feeds' identity choices are reasonable, so this isn't "incidental" (0–2), but it does break one full feed outright with no acknowledgment — landing at the low end of "works for most items; the feed it breaks on is unaddressed or unnoticed."

### Ambiguity handling — 4 / 8

Only one of the three named forks in the rubric is actually surfaced, in the `store.py` module docstring:

```python
"""SQLite persistence — deduplication and send tracking.

Design decisions (2026-08-16):
- `items` uses a UNIQUE constraint on (source, raw_id) so we never insert the
  same feed entry twice, even if cron fires repeatedly.
- `sent_items` tracks which (item, channel) pairs have already been delivered,
  so each channel only receives a given item once.
- We use INSERT OR IGNORE for items to make repeated fetches idempotent.
"""
```

Per-channel vs. global suppression: named and resolved correctly (`sent_items` is keyed on `(item_id, channel)`, verified: `energy` and `ops` both still receive items that `everything` also receives, because suppression is per-channel — see the snapshot-a run output showing the same items in `ops`, `energy`, and `everything`).

Edited-item-as-new vs. same: decided silently by the choice of `raw_id` as the key (confirmed: `wire-2026-08-14-0031` → `wire-2026-08-14-0031-r2` between snapshots resends the edited item as new), but never named as a decision anywhere.

First-run backfill: never discussed at all. On a first run (or on the legacy-DB scenario below, which is functionally a first run against the new tracking table), every historical item in every feed is sent as if new. Given the job runs every 15 minutes and is described as already running, this is a real operational question left completely unaddressed.

That's one fork named-and-correct, one silent-but-defensible, and one silent-and-untested-and-actually-broken (the `blogroll` no-id case, which isn't one of the three canonical forks but is adjacent to "edited item as new vs same" — here it's "item with no id at all," and the silent decision is simply wrong).

### Failure-mode reasoning — 5 / 8

Marking is per-channel and happens only after the channel send succeeds:

```python
# digest.py
channels.send(chan_cfg, body)
store.mark_sent(db, [i["id"] for i in selected], chan_cfg["name"])
```

If channel B raises (channels.py lets `DeliveryError`/`URLError` propagate), channel A's items are already marked sent and won't resend; channel B's items remain unmarked and will be retried on the next cron tick. That's the correct at-least-once ordering and it is per-channel, which is the right shape — but there is no comment or README text arguing for at-least-once over at-most-once, and no discussion of what an operator sees when the process dies mid-loop (it will crash with an uncaught traceback and a non-zero exit, same as before the change — not a regression, but also not discussed as part of "fixing" the duplicate problem). This matches "per-channel marking, sane ordering, no discussion."

### Existing-code respect — 2 / 6

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are untouched; `--dry-run` still works and correctly avoids marking anything sent (verified: running `--dry-run` twice in a row against the same DB produces identical output both times). The `items` archive is reused rather than replaced. That part is good.

But the schema "migration" is just `CREATE TABLE IF NOT EXISTS`, which does nothing to an existing `items` table:

```sql
CREATE TABLE IF NOT EXISTS items (
    ...
    raw_id      TEXT NOT NULL,
    ...
    UNIQUE(source, raw_id)
);
```

Reproduced against a database built from the *original* schema (nullable `raw_id`, no `UNIQUE`) — i.e., the actual state of the database this cron job has presumably been writing every 15 minutes since before this change:

```
$ sqlite3 digest.sqlite3 '.schema items'   # legacy schema, pre-existing
CREATE TABLE items (id ..., raw_id TEXT, ... );   -- no UNIQUE, raw_id nullable

$ python3 digest.py --config config.example.toml --db digest.sqlite3   # run 1
sent 12 items
$ python3 digest.py --config config.example.toml --db digest.sqlite3   # run 2, same feed snapshot
sent 12 items    # every item resent again — the exact bug this task was to fix
$ sqlite3 digest.sqlite3 'select id,source,raw_id,title from items'
1|newsroom|84121|Regulator opens inquiry into port fees
...
7|newsroom|84121|Regulator opens inquiry into port fees   # duplicate row, new id
...
```

Because `CREATE TABLE IF NOT EXISTS` silently no-ops, the legacy table keeps its old (nullable, non-unique) column definitions forever. `INSERT OR IGNORE` then has no conflict to ignore, so every fetch inserts fresh rows with fresh ids for items already seen, and `sent_items` (keyed on those ids) can never catch up — the duplicate-notification bug this task exists to fix is completely unfixed on any database that predates this change. Since the prompt states the job already runs from cron every 15 minutes, this is very likely the exact deployment scenario, not an edge case.

### Code quality — 2 / 4

The new code itself is readable and reasonably organized (clear function names, docstrings on each). No dead code, no vestigial columns were introduced. But `raw_id TEXT NOT NULL` is not "sensible SQL" given `feeds.py` documents a feed format with no id — this is a schema/logic mismatch inside the same PR, not a pre-existing condition, and it silently deletes data (see Identity strategy above). That plus the missing migration keeps this out of full marks.

### Documentation — 1.5 / 4

`README.md` is byte-for-byte identical to the original — `diff` produces no output. The only documentation of the dedupe design is the `store.py` module docstring reproduced above, which:
- does explain the per-channel and unique-constraint choices,
- does not mention how to reset the dedupe state (e.g., for a re-send or a channel migration),
- does not mention what happens on first run,
- does not mention the migration story for an existing `.sqlite3` file,
- does not appear in the README's "Layout" table description of `store.py`, which still reads "SQLite archive of everything seen" with no reference to send-tracking at all.

Per the rubric, a docstring alone caps this at 2; given it also omits reset/first-run/migration guidance entirely, it sits just under that cap.

## 3. What it missed

- **Schema migration for the pre-existing database.** This is the single biggest miss: `CREATE TABLE IF NOT EXISTS` cannot add a `UNIQUE` constraint or tighten a nullable column on a table that already exists, and the task's own framing ("we run digest.py from cron every 15 minutes") all but guarantees such a table already exists. No `ALTER TABLE`, no versioned migration, no even a comment acknowledging the gap.
- **The no-id feed.** `feeds.py` flags `blogroll` as having no stable identifier in a comment right next to the code the agent read and left alone. The agent's schema change directly contradicts that documented fact and was never tested against it (an `--dry-run` or one real run against `config.example.toml`, which includes the `blogroll` feed, would have shown zero blogroll items ever appearing).
- **First-run / backfill behavior.** Never discussed. Whether flooding every channel with the full historical backlog on first activation (or after the DB is reset) is desired is left for the next person to discover the hard way.
- **README.** Not touched at all, despite the task explicitly asking for the codebase to be left "in a state where the next person to touch it understands what you chose and why." The only design rationale lives in one file's docstring.
- **No warning/telemetry on dropped items.** When `INSERT OR IGNORE` silently drops a row (whether due to the `NOT NULL` bug or a genuine duplicate), there is no log line distinguishing "already sent, ignored as expected" from "failed to insert, lost silently."

## 4. Bugs

**Bug 1 — `blogroll` items are silently and permanently dropped, never sent, on every run (even a brand-new database).**

Reproduction:
```sh
cd <fresh clone>
python3 digest.py --config config.example.toml --db digest.sqlite3
# Firehose ("everything", keywords=[]) shows only 4 items (newsroom x2, wire x2)
sqlite3 digest.sqlite3 'select source from items'
# newsroom / newsroom / wire / wire  — no "blogroll" rows at all
```
Root cause: `store.py`'s `raw_id TEXT NOT NULL` conflicts with `feeds.py`'s `blogroll` format, which always sets `"raw_id": None`. `INSERT OR IGNORE` swallows the constraint violation with no error and no log message.

**Bug 2 — the fix does not activate against an existing/legacy database, so the original duplicate-spam bug persists unchanged in the most likely deployment.**

Reproduction:
```sh
# Build a DB with the *original* (pre-fix) schema, as an already-running deployment would have:
sqlite3 digest.sqlite3 "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, raw_id TEXT, title TEXT NOT NULL, link TEXT NOT NULL, summary TEXT, published TEXT, first_seen TEXT NOT NULL DEFAULT (datetime('now')));"
python3 digest.py --config config.example.toml --db digest.sqlite3   # -> sent 12 items
python3 digest.py --config config.example.toml --db digest.sqlite3   # -> sent 12 items again, same feed data
sqlite3 digest.sqlite3 'select count(*) from items'   # 24 rows for what should be 12 unique items
```
Root cause: `CREATE TABLE IF NOT EXISTS` in `store.connect()` does not modify an existing `items` table, so the new `UNIQUE(source, raw_id)` constraint and `NOT NULL` never get applied; `INSERT OR IGNORE` has nothing to conflict against, so every fetch inserts brand-new rows with brand-new ids, and `sent_items` (which tracks by id) can never suppress them.

```json
{
  "identity_strategy": 5,
  "ambiguity_handling": 4,
  "failure_mode_reasoning": 5,
  "existing_code_respect": 2,
  "code_quality": 2,
  "documentation": 1.5,
  "total": 19.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Right shape, but migration missing and silently drops the no-id feed."
}
```
