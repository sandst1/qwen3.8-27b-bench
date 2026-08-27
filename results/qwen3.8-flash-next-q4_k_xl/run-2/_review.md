# Review: notify-digest dedup fix

## 1. Summary

The agent replaced the raw-id/guid dedupe idea entirely with a tracking-param-stripped-link identity (`feeds.item_key`), added a per-channel `delivered` ledger written only after a send succeeds, and left the archive `items` table deduplicated instead of growing unbounded. I ran it against both fixture snapshots and a simulated channel outage and everything behaved exactly as documented — no duplicate re-sends, correct retry-after-failure semantics, and no regression to `--dry-run` or the untouched `channels.py`/`render.py`. I would merge this with two follow-ups: document how to reset the ledger, and warn ops that the first run after deploying this fix will re-send everything currently live in the feeds (the new ledger has no memory of what the *old*, buggy code already delivered).

## 2. Per-category scoring

### Identity strategy — 9.5 / 10

`feeds.py` states the three failure modes up front and picks a single fallback chain (normalized link → `raw_id` → title+published) that survives all of them:

```python
def item_key(item):
    """Stable per-source identity for an item. ..."""
    link = _normalize_link(item.get("link", ""))
    if link:
        return link
    if item.get("raw_id"):
        return f"id:{item['raw_id']}"
    return f"title:{(item.get('title') or '').strip()}|{item.get('published') or ''}"
```

I verified all three named failure modes against the actual fixtures:

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # same snapshot, next tick
sent 0 items
$ python3 digest.py --config /tmp/cfg-b.toml --db digest.sqlite3   # snapshot-b
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```
Only the genuinely new newsroom article (`entry_id: 84130`) was sent. The `utm_campaign` rotation (`w33`→`w34`), the wire `guid` regeneration (`wire-...-0031` → `wire-...-0031-r2`), and the blogroll title/excerpt edit were all correctly recognized as "same article" and suppressed — exactly the three failure modes the docstring calls out.

Deduction: the docstring itself says `newsroom entry_id is stable`, yet `item_key()` never actually prefers `raw_id` for newsroom — it always tries the normalized link first, only falling back to `raw_id` when the link is empty. Since newsroom always has a link, `entry_id` is dead weight in practice. If a newsroom retitle ever changes the URL slug (common in slug-from-headline CMSes, not exercised by the fixtures), the "known-stable" id is bypassed and the article would be treated as new. Not a fixture-provable bug, but an inconsistency between what the docstring claims and what the code does.

### Ambiguity handling — 7.5 / 8

All three named forks are resolved explicitly, in code and in the README:

- **Per-channel vs global** — `store.py`: `delivered` is keyed `(channel, source, dedup_key)`, and the README states it plainly: *"Tracking is per channel, not global: each recipient gets every article exactly once, and a channel added later still gets a first-time backfill of the items it matches."* I confirmed the backfill claim by adding a channel after run 1 with an unchanged feed — it got the full current batch (`sent 6 items`) while the existing channels correctly got nothing.
- **Edited item as new vs same** — README, explicit: *"an edited article (new guid, new title, new utm tag) is correctly treated as already sent and not re-notified — which is what we want."* Confirmed above (blogroll/wire edits suppressed).
- **First-run backfill** — covered, but only through the "new channel" lens (`README.md:40`); there's no explicit sentence for the literal "very first invocation of the whole system, empty ledger" case, even though it's the same mechanism. Half-point deduction for that gap in framing rather than a wrong decision.

### Failure-mode reasoning — 7.5 / 8

`digest.py:68-71`:
```python
channels.send(chan_cfg, body)
# Only mark as delivered once the send actually succeeded, so a channel
# that was down retries the same items on the next tick.
store.mark_delivered(db, chan_cfg["name"], selected)
```
This is per-channel, ordered correctly (mark after send, not before), and the tradeoff is argued in prose (favor redelivery over silent loss when a channel is down). I verified the actual partial-failure window: one working `stdout` channel and one webhook pointed at a closed port.

```
run 1: ok-channel delivered (2 items), bad-channel raised ConnectionRefusedError, process exited 1
       delivered table afterward: only ok-channel's two rows
run 2 (bad-channel fixed): only bad-channel gets resent; ok-channel is correctly NOT duplicated
```
That's exactly the intended at-least-once-per-failing-channel / at-most-once-per-succeeding-channel behavior, and it's the correct choice for a notifier. Deduction: the tradeoff is argued informally ("retried instead of lost") but never named (at-least-once vs at-most-once), and the narrow crash window between a successful `channels.send()` and the `conn.commit()` in `mark_delivered` (a duplicate on process-kill mid-write) isn't discussed.

### Existing-code respect — 6 / 6

`feeds.fetch()` / `feeds._load()` are byte-identical to the original (diffed and confirmed no changes below the new `item_key` block). `channels.py`, `render.py`, and `config.example.toml` are untouched. `--dry-run` was verified to still short-circuit before `mark_delivered` and before `channels.send()`:
```
$ python3 digest.py --config config.toml --db digest.sqlite3 --dry-run   # x2
sent 0 items          # both times
delivered rows: 0     # confirmed no ledger writes happened
```
The unconditional-insert problem in `store.record_items` (flagged by name in the rubric) is directly addressed:
```python
"INSERT OR IGNORE INTO items (source, raw_id, dedup_key, title, link, summary, published)"
```
backed by a real unique index (`idx_items_dedup` on `(source, dedup_key)`), so the archive table stops growing on every poll. No scope creep — no scheduler, no web UI, no plugin system.

### Code quality — 4 / 4

Readable, commented at the "why" level rather than the "what" level, parameterized SQL throughout, `executemany` used consistently. Migration is handled and I verified it against a genuinely pre-existing (pre-dedup-key) database:
```python
def _migrate(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "dedup_key" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN dedup_key TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_dedup ON items(source, dedup_key)")
```
Ran clean against an old-schema seeded DB with no crash, column added, index created (see Bugs section for the one real side effect of this).

### Documentation — 3 / 4

The README adds a full "How duplicate delivery is prevented" section with a per-feed failure-mode table and a reproduction script. I ran the exact commands from the README verbatim and they work as documented:
```
$ python3 digest.py --config config.example.toml --db /tmp/d.db   # first run
sent 12 items
$ python3 digest.py --config config.example.toml --db /tmp/d.db
sent 0 items
$ python3 digest.py --config /tmp/cfg-b.toml --db /tmp/d.db
sent 2 items
```
This clears the "docstring alone caps at 2" bar comfortably. But the rubric asks for three specific things and one is entirely absent: **how to reset the dedup state**. There's no mention of deleting `digest.sqlite3`, truncating `delivered`, or any other way to force a resend for, e.g., testing or an operator complaint of "I want this again." "What happens on first run" is present only implicitly (via the new-channel-backfill note), not stated for the literal first invocation.

## 3. What it missed

- **No reset instructions** — an operator who wants to re-trigger delivery of an already-sent item (a common ask: "resend the digest from this morning") has no documented path. The mechanism (`DELETE FROM delivered WHERE ...`) is trivial but unstated.
- **No upgrade-path warning for the ledger's cold start.** The `items` archive gets a real migration (`_migrate`); the new `delivered` ledger does not — it starts empty on first run against *any* database, including one from a system that has been running the old, buggy code for months. See Bugs below for the reproduction; this is exactly the symptom the task was filed to fix, and it isn't called out anywhere.
- **Cross-run overlap** (a slow poll still running when cron fires again 15 minutes later) is not addressed, but it wasn't addressed originally either, and SQLite's own locking gives some accidental protection; a note either way would have been cheap.
- **`newsroom`'s "stable" `entry_id`** is described but not actually used as the primary key for that feed (see Identity strategy above) — a design note explaining why link-first was chosen over the feed's own stable id would have preempted the question.
- **No automated tests** were added; correctness is only demonstrated by the (very good) README repro script and by hand-verification, not by anything that runs in CI.

## 4. Bugs

**Not a crash, but a real "same item over and over, one more time" event on deploy.** The new `delivered` ledger has zero knowledge of what the *previous* (buggy) code already delivered. Deploying this fix to a live system means the very next cron tick re-sends every article currently present in the feeds' response windows — precisely the complaint in the original bug report, occurring one final time, silently, with no warning in the README or a migration note.

Repro (old-schema DB standing in for "production, already running for a while"):
```sh
python3 -c "
import sqlite3
conn = sqlite3.connect('prod.sqlite3')
conn.executescript('''
CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, raw_id TEXT, title TEXT NOT NULL, link TEXT NOT NULL,
    summary TEXT, published TEXT, first_seen TEXT NOT NULL DEFAULT (datetime('now'))
);
''')
for i in range(5):
    conn.execute(\"INSERT INTO items (source, raw_id, title, link) VALUES ('newsroom','84121','x','y')\")
conn.commit()
"
python3 digest.py --config config.toml --db prod.sqlite3
# -> sent 12 items, even though this "production" db already has 5 rows
#    recording that newsroom/84121 was seen (and, in real life, delivered)
#    before the fix was deployed.
```
This is a one-time, self-healing event (subsequent ticks dedupe correctly), so it's minor in absolute terms, but it is the exact bug the task asked to fix, recurring once more, undocumented.

A second, smaller side effect of the same gap: migrating an existing `items` table leaves the pre-migration rows permanently un-deduplicated against post-migration rows, because SQLite's unique index treats `NULL != NULL`:
```
cols: ['id', 'source', 'raw_id', 'title', 'link', 'summary', 'published', 'first_seen', 'dedup_key']
rows:
{'source': 'newsroom', 'raw_id': '84121', 'dedup_key': None, ...}                                    <- old row, untouched
{'source': 'newsroom', 'raw_id': '84121', 'dedup_key': 'https://newsroom.example/2026/08/port-fees', ...}  <- new row, same article
```
The `_migrate` docstring is honest that this is why the index "never fails," but doesn't mention that it also means old rows never get merged with new ones. Since nothing reads `items` except `count_items` (unused), this has no delivery impact — it's purely an archive-table cosmetic issue.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 7.5,
  "failure_mode_reasoning": 7.5,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 3,
  "total": 37.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid link-based dedup and per-channel ledger; undocumented reset and deploy-day resend."
}
```
