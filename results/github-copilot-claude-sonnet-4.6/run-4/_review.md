# Review — notify-digest, run-4

## 1. Summary

The agent added a "seen" check keyed on `(source, link)` enforced by a new
`UNIQUE` constraint on the existing `items` table, and made `digest.py` record
items before sending so a crash mid-send can't duplicate a batch — a small,
surgical diff that leaves `feeds.py`, `channels.py`, and `render.py` untouched
and keeps `--dry-run` working. The chosen key breaks on exactly the trap the
fixtures set up (`utm_campaign` rotates on every newsroom item, changed or
not), so two of the four newsroom items get resent verbatim on the very next
poll — reproduced below — and the "record-before-send" ordering, combined
with a single un-scoped exception in `channels.send`, silently and
permanently drops a whole feed's items for every channel if even one channel
fails partway through a run. I would not merge this as-is: it fixes the
easy 80% of the problem but reintroduces the reported symptom for the
newsroom feed and adds a new, worse failure mode (data loss) that isn't
disclosed anywhere.

## 2. Per-category scoring

### Identity strategy — 4 / 10

`store.py:7-24` lays out real reasoning for rejecting `raw_id` (missing on
blogroll, regenerated on generic/wire edits) and `title` (gets corrected), in
favor of `link`:

```python
# store.py:18-20
  link    — the canonical URL of the item.  All three feed formats always
             provide one, and once published, links do not change.  This
             is the best key we have across all providers.
```

That reasoning is wrong for the `newsroom` feed specifically. Its URLs carry
a `utm_campaign` parameter that changes from week to week for *unchanged*
items — this is baked into the fixtures on purpose:

```
snapshot-a: https://newsroom.example/2026/08/port-fees?utm_source=feed&utm_campaign=w33
snapshot-b: https://newsroom.example/2026/08/port-fees?utm_source=feed&utm_campaign=w34
```

Reproduction (fresh DB, run against snapshot-a, then point the same DB at
snapshot-b — simulating the next cron tick):

```
$ python3 digest.py --config config.toml --db digest.sqlite3      # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3      # snapshot-a again
sent 0 items                                                       # correct, no dup
$ sed -i '' 's/snapshot-a/snapshot-b/' config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3      # snapshot-b
=== ops ===
* Regulator opens inquiry into port fees  [newsroom]     <-- SAME ITEM, entry_id 84121, resent
  https://newsroom.example/2026/08/port-fees?utm_source=feed&utm_campaign=w34
=== energy ===
* Grid operator delays offshore tender  [newsroom]        <-- SAME ITEM, entry_id 84118, resent
  https://newsroom.example/2026/08/offshore-tender?utm_source=feed&utm_campaign=w34
sent 6 items
```

Both items were already delivered in the first run with `utm_campaign=w33`;
they come back byte-identical except for the query string. This is the exact
scenario the task exists to fix ("people keep getting the same items over and
over"), and it is unfixed for this feed. Meanwhile the wire feed's edited-guid
case and the blogroll's edited-title case (both keep the same `link`) *are*
correctly suppressed — so the strategy is not naive across the board, it is
naive specifically about query-string volatility, which is one of the three
named traps in the rubric. That lands this in the "breaks on a feed the agent
didn't notice" territory, weighted down because the failure is total (the
newsroom feed will keep re-triggering on every campaign rotation, not a one-
off).

### Ambiguity handling — 2 / 8

Three forks exist; the agent surfaces at most one, and gets the second wrong.

1. **Edited item as new vs. same** — addressed, if implicitly: choosing
   `link` over `raw_id`/title means edited items are treated as the same
   item and not resent. Reasoned about in the docstring quoted above. This
   is the one credit here.
2. **Per-channel vs. global suppression** — never named. `run_once` marks an
   item seen once, at the feed level, *before* the channel loop runs at all:

   ```python
   # digest.py:51-59
   unseen = store.filter_unseen(db, feed_cfg["name"], items)
   if not dry_run:
       store.record_items(db, feed_cfg["name"], unseen)
   new_items.extend(unseen)
   ```

   There is no concept of "sent to channel X" — an item that matches zero
   channels' keyword filters is still marked seen forever, and an item that
   was supposed to go to two channels but only reached one (see Bugs, below)
   is gone for both. This is the "typically global suppression" failure the
   rubric calls out by name, and it is not discussed anywhere in code or
   docs.
3. **First-run backfill** — not discussed at all. On an empty DB every
   current item in every feed is dispatched immediately (verified: first run
   against snapshot-a sent 12 items across three channels). That's a
   defensible default but nothing acknowledges it as a choice, and it will
   surprise the ops-box operator the first time they attach a feed with a
   long backlog.

### Failure-mode reasoning — 3 / 8

The ordering choice is stated and has a rationale:

```python
# digest.py:54-57
if not dry_run:
    # Mark them seen now, before sending, so a crash mid-send does not
    # cause a double-delivery on the next cron tick.
    store.record_items(db, feed_cfg["name"], unseen)
```

That is a real choice (at-most-once over at-least-once), but it is argued
one-sidedly: the comment presents it as pure upside ("does not cause
double-delivery") and never mentions the cost, which is severe. Marking
happens once per feed, before any channel is attempted, and `channels.send`
lets `DeliveryError` propagate uncaught all the way to `main()`, crashing the
whole process:

```
$ python3 digest.py --config test_config.toml --db test.sqlite3
channels.DeliveryError: broken-webhook: <urlopen error [Errno 61] Connection refused>
exit: 1
$ sqlite3 test.sqlite3 "select count(*) from items"
2
$ python3 digest.py --config test_config.toml --db test.sqlite3   # retry, same feed
sent 0 items
```

Two items were recorded as seen before the broken webhook channel raised.
The crash meant `backup-stdout` — the channel listed *after* the broken one,
which would have succeeded — never even got a chance to run in that pass.
On the next cron tick those items are gone for every channel, forever, with
no log line indicating anything was lost. This is precisely the "partial-
failure window that loses ... a whole batch" case the rubric names, and nothing
in the diff catches per-channel exceptions or marks per-channel — it's a
single global mark for the whole feed, done before any delivery is attempted.

### Existing-code respect — 5 / 6

The diff is small and targeted: `feeds.py`, `channels.py`, and `render.py` are
untouched, the existing `items` table is extended in place rather than
replaced, and `--dry-run` is respected by design (`record_items` is skipped
in dry-run, verified: two consecutive dry runs against the same DB both show
the same candidate items, and the DB row count stays at 0). No scope creep.
Half a point off for the schema-migration gap discussed below, which is a
symptom of not fully engaging with "this runs against an existing DB in
production already."

### Code quality — 2.5 / 4

Readable and free of dead code. The deduction is for schema migration: the
new `UNIQUE (source, link)` constraint is added inside `CREATE TABLE IF NOT
EXISTS`, which is a no-op against any database that already exists (which,
per the *original* `store.py` docstring, is the deployed reality — this runs
from cron already). Reproduced:

```
$ python3 -c "
import sqlite3
c = sqlite3.connect('old.sqlite3')
c.executescript('CREATE TABLE items (id INTEGER PRIMARY KEY, source TEXT, ..., link TEXT NOT NULL, ...)')
"
$ python3 -c "import store; store.connect('old.sqlite3')"
$ sqlite3 old.sqlite3 ".schema items"   # still no UNIQUE constraint
```

`filter_unseen` still works functionally on such a DB (it does a plain
`SELECT`, not relying on the constraint), so this isn't a crash — but the
docstring's claim that "the UNIQUE constraint ... lets SQLite enforce this at
the database level" is simply false for any pre-existing installation, and
there's no `ALTER TABLE` / migration step to fix that up. On a fresh DB it's
fine; on the box this is actually deployed to, the constraint silently never
takes effect.

### Documentation — 2 / 4

`README.md` is byte-for-byte identical to the original — `diff` confirms
zero changes. All of the explanation lives in docstrings in `digest.py` and
`store.py`. Per the rubric, "a docstring alone caps this at 2" — that's what
happened here. The docstrings themselves are genuinely good (the `store.py`
one walks through why `raw_id` and `title` were rejected), but a reader
following the README's "Layout" table to understand the system would see
nothing about dedupe, how to reset it, or what a first run does.

## 3. What it missed

- The `utm_campaign` rotation trap (Identity strategy) — the agent's own
  docstring anticipated volatility in `raw_id` but not in the URL it chose as
  the replacement key.
- Per-channel suppression is never considered as a fork; the fix suppresses
  at the feed level, before channels are even consulted.
- First-run backfill behavior (send everything immediately) is unremarked.
- The cost side of "mark before send" (permanent loss on any channel
  failure) is not mentioned, only the benefit side.
- No migration for pre-existing databases — the new constraint is inert on
  any DB this code is actually likely to be run against.
- README left completely untouched, despite the task's explicit ask to leave
  the codebase "so the next person understands what you chose and why."

## 4. Bugs

**Bug 1 — newsroom items resent after a campaign-param rotation** (see
Identity strategy above for full repro). Concretely: `entry_id 84121` and
`84118` are sent in run 1 (query string `utm_campaign=w33`) and sent again in
run 2 against snapshot-b (`utm_campaign=w34`), because dedupe keys on the raw
`link` string. This is the literal bug the task asked to fix, still present
for one of three feeds.

**Bug 2 — permanent cross-channel data loss on any single channel failure.**
Repro:

```
[[channels]]
name = "broken-webhook"
type = "webhook"
url = "http://127.0.0.1:1/nope"
keywords = []

[[channels]]
name = "backup-stdout"
type = "stdout"
keywords = []
```

```
$ python3 digest.py --config test_config.toml --db test.sqlite3
... DeliveryError: broken-webhook: <urlopen error [Errno 61] Connection refused>
exit code 1
$ sqlite3 test.sqlite3 "select count(*) from items"   # 2 rows already committed
2
$ python3 digest.py --config test_config.toml --db test.sqlite3   # cron retries
sent 0 items
```

`backup-stdout` never ran in the first pass (the exception aborted the
process before the channel loop reached it), and on the retry both items are
already marked "seen" — so they are never delivered to *any* channel,
including the one that was healthy the whole time. The task's premise
("people keep getting the same items over and over") is swapped for its
mirror image ("people silently never get some items at all") whenever a
channel hiccups.

```json
{
  "identity_strategy": 4,
  "ambiguity_handling": 2,
  "failure_mode_reasoning": 3,
  "existing_code_respect": 5,
  "code_quality": 2.5,
  "documentation": 2,
  "total": 18.5,
  "would_merge": "no",
  "one_line": "Fixes same-run dupes but resends on utm rotation and loses items on channel failure."
}
```
