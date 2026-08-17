# Review — claude-sonnet-4.6-max, run-1

## 1. Summary

The agent added a `store.seen_links()` lookup and filtered each feed's items
against it before adding them to `all_items`, moving the DB write to record
only the new items — a small, surgical diff that fixes the write-only-archive
bug and is backed by a genuinely useful `test_dedup.py`. It chose `link` (URL)
as the dedup key for all three feed formats and documented that choice, but
the choice is provably wrong for the `newsroom` feed (whose URLs rotate a
`utm_campaign` query param — exactly the failure mode the task exists to
avoid), and the implementation marks items as delivered *before* any channel
send is attempted, which silently corrupts state under `--dry-run` and drops
items on partial channel failure. I would not merge this as-is: the core idea
is sound but two of the three real regressions it introduces are exactly the
kind of "same item over and over" / "item lost forever" bugs the task was
about, and neither is mentioned anywhere.

## 2. Per-category scoring

### Identity strategy — 4.5 / 10

`digest.py:41-48`:
```python
# Only keep items whose link we have not recorded before.
# `link` (URL) is the dedup key for all three feed formats — see the
# docstring in store.py for why raw_id / guid is not reliable here.
known = store.seen_links(db, feed_cfg["name"])
new_items = [i for i in items if i["link"] not in known]
```

`store.py:11-21` reasons about it explicitly:
```
* The *blogroll* feed has no stable identifier at all (``raw_id`` is always
  ``None``).
* The *wire* (generic) feed regenerates its guid whenever an item is edited
  ...its URL, however, does not change.
* The *newsroom* feed has a stable ``entry_id``, but ``link`` works just as
  well and keeps the dedup logic uniform across all three sources.
```

The wire and blogroll cases are correctly reasoned and I verified them by
replaying `fixtures/snapshot-a` then `snapshot-b` through the tool: a
guid-regen on the wire feed and a title/excerpt edit on blogroll correctly do
**not** re-fire.

But the newsroom claim ("link works just as well") is false, and the fixtures
prove it. `fixtures/snapshot-a/newsroom.json` and `snapshot-b/newsroom.json`
carry the *same* `entry_id` for the same articles (84121, 84118) but a
rotated `utm_campaign` param in the URL (`w33` → `w34`). Feeding snapshot-a
then snapshot-b through the actual tool:

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 6 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a again
sent 0 items
$ # point config at snapshot-b, run again
sent 3 items
* Union responds to port fee inquiry        [newsroom]   (genuinely new)
* Regulator opens inquiry into port fees    [newsroom]   (SAME entry_id 84121, RESENT)
* Grid operator delays offshore tender      [newsroom]   (SAME entry_id 84118, RESENT)
```

Two of three "new" items in the snapshot-b digest are duplicates of items
already sent in snapshot-a — the exact bug the cron job was reported for.
This is the rubric's own named trap ("survives rotating `utm_campaign`") and
the agent's own docstring notices the safer field (`entry_id`) exists and
picks the worse one anyway, with no test covering it. `test_dedup.py` tests
guid-rotation for wire and no-raw_id for blogroll, but has no test for
newsroom's query-string rotation — the one place the identity choice
actually breaks.

### Ambiguity handling — 3 / 8

Three forks exist in this domain; one is named, one is decided silently and
wrongly, one is decided silently but is plausibly fine:

- **Edited item as new vs. same** — named and resolved, in the `store.py`
  docstring quoted above (treat as same for wire/blogroll). This is the one
  fork done right and documented.
- **Per-channel vs. global suppression** — decided silently, and wrongly.
  `seen_links` is keyed only by `source` (`store.py:56-58`), not by channel,
  and the record happens once per feed before the per-channel filtering loop
  (`digest.py:44-48`). If a channel's keyword filter later changes such that
  an older item would now match, that item can never be delivered to that
  channel — it was already recorded as seen the first time any channel saw
  it. Nothing in the code or docs acknowledges this is global, not
  per-channel, suppression.
- **First-run backfill** — decided silently (send everything on an empty
  DB, per `test_first_run_sends_all_items`), which is a reasonable default,
  but there is no comment or README line saying so, so an operator deploying
  this for the first time has no way to know a full backlog dump is coming.

Only one of three forks is both named and correct; the global-suppression
one is the rubric's own example of "at least one decision is wrong."

### Failure-mode reasoning — 2 / 8

The write to the dedup store happens before any delivery is attempted at
all, for the whole feed, once per run — not per-channel, and not
conditioned on `dry_run`:

`digest.py:44-48`:
```python
known = store.seen_links(db, feed_cfg["name"])
new_items = [i for i in items if i["link"] not in known]
store.record_items(db, feed_cfg["name"], new_items)   # <-- committed here
all_items.extend(new_items)
```
...then later, in a completely separate loop:
```python
for chan_cfg in cfg["channels"]:
    ...
    channels.send(chan_cfg, body)
```

`store.record_items` calls `conn.commit()` immediately (`store.py:71`). I
reproduced two consequences of this ordering:

1. **Partial channel failure loses the batch.** With two channels
   configured, if channel 1 succeeds and channel 2 raises
   `channels.DeliveryError`, the item is already committed as "seen" before
   channel 2 is even reached, and the exception propagates out of
   `run_once` uncaught. On the next cron tick, the item is gone for good —
   channel 2 never gets it, and there's no retry.

   ```
   sent to c1
   run_once raised: simulated webhook down
   seen links after crash: {'http://x/1'}
   ```

2. **`--dry-run` corrupts real dedup state.** `record_items` runs
   unconditionally, before the `dry_run` check, so a preview run pollutes
   the DB exactly as a real send would:

   ```
   $ python3 digest.py --config config.toml --db dry.sqlite3 --dry-run
   sent 0 items
   $ python3 -c "import store; print(store.count_items(store.connect('dry.sqlite3')))"
   7
   $ python3 digest.py --config config.toml --db dry.sqlite3     # real run, same DB
   sent 0 items
   ```

   Every item previewed once with `--dry-run` is now permanently
   unsendable. This is exactly the "loses ... a whole batch" failure the
   2–3 band describes, and it is not discussed anywhere.

There is no per-channel marking, no argument for at-least-once vs.
at-most-once, and the existing channels.py comment ("let the exception
propagate and cron will pick us up again on the next tick") is now false for
anything already recorded on this run.

### Existing-code respect — 3 / 6

The diff is small and does not touch `feeds.py`, `channels.py`, or
`render.py` at all — good restraint, and it correctly deals with the
"unconditional insert" the rubric calls out (only new items are now written
to `items`, instead of every item on every run). But `--dry-run` is
explicitly named in the rubric as a specific thing not to break, and it is
broken (see reproduction above): a preview run has the same side effect on
the dedup store as a real send. That is a direct, confirmed regression in
existing, working behavior, and it's silent — nothing in code, tests, or
README flags it.

### Code quality — 3.5 / 4

Code is clean, the new `seen_links` function is small and well-named, no
dead code or vestigial columns, and no schema migration was needed (no
column changes). Minor: `seen_links` loads every link ever recorded for a
source into memory on every 15-minute tick with no bound or pruning
(`store.py:56-59`); fine at current scale, unaddressed for the long run.

### Documentation — 2 / 4

`README.md` is byte-for-byte unchanged from the original (verified with
`diff`). All of the (good) reasoning about dedup keys lives in the
`store.py` docstring and `test_dedup.py` docstring, never in the README. Per
the rubric, "a docstring alone caps this at 2" — there is nothing in the
README about the dedupe behaviour, how to reset it (e.g. delete the sqlite
file, or `DELETE FROM items WHERE source=...`), or what happens on first
run (full backlog dispatch).

## 3. What it missed

- Global vs. per-channel suppression was never named as a decision at all.
- First-run backfill behaviour (send everything) is implemented but never
  called out to an operator.
- The `--dry-run` regression was not caught by the agent's own tests, which
  never dry-run at all.
- The newsroom `utm_campaign` rotation was not caught by the agent's own
  tests, which only cover wire guid-rotation and blogroll no-raw_id — the
  two cases it happens to get right, not the one it gets wrong.
- No mention of unbounded growth of `seen_links`'s working set over the
  life of the deployment.

## 4. Bugs

**Bug 1 — newsroom items resend when `utm_campaign` rotates in the URL.**
Repro: run against `fixtures/snapshot-a`, then point the same DB at
`fixtures/snapshot-b`. Newsroom entries 84121 and 84118 (stable `entry_id`,
per the feed's own data) are re-sent because their `link` field's query
string changed from `utm_campaign=w33` to `utm_campaign=w34`. See identity
strategy section for the full transcript.

**Bug 2 — `--dry-run` corrupts the dedup store.** Repro: run
`digest.py --dry-run` once against a fresh DB, then run it again without
`--dry-run`. The second, real run sends nothing, because the dry run already
recorded every item as seen. See failure-mode section for the transcript.

**Bug 3 — a failing channel loses items for all channels, permanently.**
Repro: configure two channels, make the second one raise on send. The items
are already committed to `items` (hence excluded from all future runs)
before the second channel is even attempted, and the exception is not
caught anywhere in `digest.py` or `channels.py`. See failure-mode section
for the transcript.

```json
{
  "identity_strategy": 4.5,
  "ambiguity_handling": 3,
  "failure_mode_reasoning": 2,
  "existing_code_respect": 3,
  "code_quality": 3.5,
  "documentation": 2,
  "total": 18,
  "would_merge": "no",
  "one_line": "Sound-looking link dedup, but breaks on newsroom, --dry-run, and partial send failure."
}
```
