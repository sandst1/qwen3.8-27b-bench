# Review: notify-digest dedup fix

## 1. Summary

The agent replaced the unconditional-archive `record_items()` with `record_new_items()`, added a `dedup_key` column with a `UNIQUE(source, dedup_key)` constraint, and picked a sensible per-feed identity (`raw_id` for newsroom, `link` fallback for blogroll/wire) that correctly survives the fixtures' rotating `utm_campaign` and regenerated `guid`. However, the change marks items "seen" at fetch time, before any channel delivery is attempted and irrespective of whether delivery succeeds, which both permanently drops items on any channel failure and silently breaks `--dry-run`'s non-destructive contract; it also ships no migration path, so it crashes outright against the already-running production database the prompt describes. I would not merge this as-is — the core identity idea is right, but it needs a fixed marking order, a fixed `--dry-run`, and a migration step before it's safe to deploy to the box it's already running on.

## 2. Per-category scoring

### Identity strategy — 9/10

The agent picked a real per-feed identity, stated its rationale in `store.py`:

```python
# store.py lines 3-14
Deduplication design
--------------------
Each item gets a ``dedup_key`` that is the authoritative "have we seen this?"
identifier for that feed format:

* newsroom   — uses ``raw_id`` (the feed's own ``entry_id``).  The URL carries
               a rotating UTM campaign tag, so the link alone is *not* stable
               across cron runs.
* blogroll   — has no provider-assigned ID at all, so we use ``link``
               (the permalink), which is stable.
* wire/generic — the provider regenerates the GUID whenever an item is edited,
               so we also fall back to ``link``.
```

And the fallback is applied uniformly:

```python
# store.py line 70
dedup_key = item["raw_id"] if item["raw_id"] else item["link"]
```

I verified this against the fixtures directly. Running snapshot-a then snapshot-b:

- Newsroom: `entry_id` 84121/84118 stay the same across snapshots while `utm_campaign` rotates `w33`→`w34` — correctly suppressed as duplicates, only the genuinely new `entry_id=84130` item was sent.
- Wire: `guid` changes from `wire-2026-08-14-0031` to `wire-2026-08-14-0031-r2` on an edited item, but `link` is stable — `feeds.py` sets `raw_id=None` for this format so the fallback catches it; correctly suppressed.
- Blogroll: title changes ("Notes on port fee arithmetic" → "... (updated)"), permalink stable — correctly suppressed.

This is a real fallback-chain identity that holds up against all three named pitfalls in the fixtures (rotating campaign tag, missing id, regenerated guid), and it says why. Docked half a point because the docstring doesn't discuss its own failure mode — e.g., a blogroll repost under a new permalink, or a substantive edit that also changes the link, will be treated as a brand-new item with no discussion of that tradeoff.

### Ambiguity handling — 3/8

Of the three canonical forks:

- **Edited-item-as-new-vs-same**: decided, and documented, but only in a code comment (`feeds.py` lines 71-74, `store.py` docstring) — never in the README or in program output. Per the rubric this is "surfacing the decision in code," which credits it, but only partially — a person running `digest.py --dry-run` or reading the README gets no hint that edits are swallowed as duplicates.
- **Per-channel vs. global suppression**: not named anywhere. The agent chose global suppression — a single `UNIQUE(source, dedup_key)` row shared across every channel:

  ```python
  # digest.py lines 41-49
  all_items = []
  for feed_cfg in cfg["feeds"]:
      ...
      new_items = store.record_new_items(db, feed_cfg["name"], items)
      all_items.extend(new_items)
  ```

  Marking happens once per feed, before any channel-specific filtering or sending. This is the "typically wrong" choice the rubric calls out by name — see the failure-mode bug below, where this exact decision causes items to be lost forever the first time a channel is unavailable.
- **First-run backfill**: not discussed at all, in code or README. On a fresh DB, the first run will archive-and-send every item currently in the feed as if it were new. Whether that is intended (a burst of stale items from provider retention) is never addressed.

One fork is named (in code only), one is silently wrong, one is silently unaddressed — solidly in the "decided silently, and at least one decision is wrong" band.

### Failure-mode reasoning — 2/8

There is no discussion anywhere of crash/retry semantics, at-least-once vs. at-most-once, or per-channel outcomes. Worse, I reproduced a genuine data-loss bug: items are recorded (and thus permanently excluded from all future runs) at fetch time, before any channel is attempted:

```python
# digest.py lines 41-49
for feed_cfg in cfg["feeds"]:
    ...
    new_items = store.record_new_items(db, feed_cfg["name"], items)   # <- marks seen here
    all_items.extend(new_items)
...
for chan_cfg in cfg["channels"]:                                       # <- delivery happens here, later
    ...
    channels.send(chan_cfg, body)
```

`channels.py`'s own docstring (unchanged by the agent) promises: "Delivery is best effort: if a channel is down we let the exception propagate and cron will pick us up again on the next tick." That promise is now false. Reproduction:

```
$ python3 - <<'EOF'
import store, feeds
db = store.connect("digest_crash_test.sqlite3")
items = feeds.fetch(newsroom_feed_cfg)
new_items = store.record_new_items(db, "newsroom", items)   # marks 3 items as seen
print(len(new_items))   # -> 3
# ... now imagine channels.send() raises DeliveryError here; process exits ...
items2 = feeds.fetch(newsroom_feed_cfg)                       # next cron tick, same data
new_items2 = store.record_new_items(db, "newsroom", items2)
print(len(new_items2))   # -> 0
EOF
3
0
```

Those 3 items were never delivered to any channel — the crash happened after marking, before sending — and they are now gone forever; the dedup key is already consumed. This is the exact "partial-failure window that loses ... a whole batch" the rubric describes. Combined with the global-suppression choice above, a failure on channel 2 also means channel 1's successful send is irrelevant — the item is now unrecoverable for every channel, not just the one that failed.

### Existing-code respect — 1.5/6

Two concrete breakages of stated codebase contracts:

**1. `--dry-run` is no longer side-effect-free.** The README says "`--dry-run` prints what would be sent instead of sending it" — implying no state change. But `record_new_items()` is called unconditionally before the `dry_run` check:

```python
# digest.py lines 41-49 (dry_run isn't checked until line 59, inside the channel loop)
new_items = store.record_new_items(db, feed_cfg["name"], items)
```

Reproduction: running `--dry-run` twice in a row against the same feed data shows the second run finds 0 new items, and a subsequent *real* (non-dry) run also finds 0 — the preview run permanently consumed the dedup key:

```
$ python3 digest.py --config config.toml --db dry.sqlite3 --dry-run   # sent 0 items (dry preview only)
$ python3 digest.py --config config.toml --db dry.sqlite3 --dry-run   # sent 0 items (no new items — already "seen")
$ python3 digest.py --config config.toml --db dry.sqlite3             # sent 0 items (real run, nothing left to send!)
```

A dry run should never be able to make a real send send nothing.

**2. No schema migration for the already-running database.** The prompt states this job already "runs from cron every 15 minutes" — i.e., there is an existing `digest.sqlite3` on the box with the pre-fix schema (no `dedup_key` column). `store.connect()` only does `CREATE TABLE IF NOT EXISTS`, which is a no-op against an existing table:

```python
# store.py lines 51-56
def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
```

Reproduction against a DB built with the *original* (pre-fix) schema:

```
$ python3 digest.py --config config.toml --db existing_digest.sqlite3
Traceback (most recent call last):
  ...
  File "store.py", line 71, in record_new_items
    cur.execute(...)
sqlite3.OperationalError: table items has no column named dedup_key
```

This is the actual deployment scenario in the prompt, and the fix crashes on it every single tick until someone manually intervenes. No `ALTER TABLE`, no migration script, no note in the README about needing to drop/recreate the DB once.

### Code quality — 2/4

The new code is readable and the comments are well-written and specific. But the same missing migration flagged above is explicitly called out in this rubric category ("schema migration handled for an existing database") and is entirely absent — `CREATE TABLE IF NOT EXISTS` plus a `NOT NULL UNIQUE` column added mid-flight is not a migration. No dead code or vestigial columns otherwise; the `raw_id` column is still stored even when `None`, which is reasonable (keeps the archive informative) rather than vestigial.

### Documentation — 1.5/4

`README.md` is **byte-for-byte unchanged** — `diff` confirms zero lines touched. All of the dedup rationale lives in a `store.py` module docstring:

```python
# store.py — the entire explanation lives here, not in README.md
"""SQLite persistence.

Deduplication design
--------------------
...
"""
```

Per the rubric, "a docstring alone caps this at 2." There is no README mention of: what dedup means for operators, how to reset/replay a feed (there is no reset mechanism — the only way is to `DELETE FROM items` by hand, undocumented), what happens on first run against a fresh DB, or the newly-introduced fact that `--dry-run` now has a permanent side effect. Given the docstring is thorough on identity but silent on all of the operational questions above, I'm scoring below the docstring cap rather than at it.

## 3. What it missed

- **Per-channel vs. global suppression** was never named as a decision; the agent picked global suppression, which directly enables the failure-mode bug above.
- **Marking-before-delivery ordering** was never considered; the contract implied by `channels.py`'s own docstring ("cron will pick us up again on the next tick") is silently broken.
- **First-run backfill** behavior (a fresh DB will archive-and-send everything currently in the feed) is not discussed.
- **`--dry-run`'s side-effect-free contract** is broken and unremarked upon.
- **Schema migration for the already-deployed database** — the scenario literally described in the prompt — was not handled at all, and crashes hard.
- **No reset/replay instructions** were added to the README for operators who want to intentionally resend an item or wipe the seen-set for a single feed.

## 4. Bugs

1. **Items lost forever on any channel delivery failure.** Reproduced above: `record_new_items()` marks items seen before any `channels.send()` call; if delivery raises (as `channels.py` explicitly anticipates), the process crashes but the items are already excluded from every future run, for every channel — not just the one that failed.
2. **`--dry-run` permanently consumes the dedup key.** Reproduced above: two consecutive `--dry-run` invocations against unchanged feed data show the second finds 0 new items, and a subsequent real run also finds 0 — a preview left nothing left to actually send.
3. **Hard crash against an existing (pre-fix-schema) database.** Reproduced above: `sqlite3.OperationalError: table items has no column named dedup_key` when pointed at a DB created with the original schema — exactly the box this cron job is already running on per the prompt.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 3,
  "failure_mode_reasoning": 2,
  "existing_code_respect": 1.5,
  "code_quality": 2,
  "documentation": 1.5,
  "total": 19,
  "would_merge": "no",
  "one_line": "Good identity fix, but crashes on the real DB and breaks --dry-run."
}
```
