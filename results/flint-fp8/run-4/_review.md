# Review: flint-fp8, run-4

## 1. Summary

The agent added a `filter_new`/`_item_key` pair to `store.py` and wired it into `digest.py`'s feed loop so that previously-seen items are dropped before rendering, gated the archive insert on `--dry-run`, and added a short paragraph to the README. The identity key (`raw_id or link`) correctly survives the `utm_campaign` rotation on the newsroom feed and the missing id on the blogroll feed, but it does **not** survive the regenerated `guid` on the wire feed — the exact case `feeds.py` warns about — and the new docstring falsely claims this case is handled. Worse, the archive write happens before any channel delivery is attempted, so a single channel failure mid-run permanently drops the batch for every channel that hadn't been reached yet; I would not merge this without both fixes.

## 2. Per-category scoring

### Identity strategy — 5 / 10

`store.py:38-45`:

```python
def _item_key(source, raw_id, link):
    """Stable identity of a feed item within a source.

    Prefers the provider's own id (`raw_id`) when it has one; the blogroll
    format has no id at all, so we fall back to the permalink. The generic
    format's `guid` is deliberately not trusted — see feeds.py.
    """
    return f"{source}:{raw_id or link}"
```

This correctly handles two of the three fixtures:
- **newsroom**: `raw_id` is `entry_id`, so the `utm_campaign` rotation in the URL (`w33` → `w34`, `fixtures/snapshot-a/newsroom.json` vs `snapshot-b`) is irrelevant to identity. Verified: entry `84121` is suppressed on the second run.
- **blogroll**: no `raw_id` ever, so it falls back to `link`. Verified: the edited post (`fixtures/snapshot-b/blogroll.json`, title now has "(updated)") is still suppressed because the permalink didn't change.

It does **not** handle the third fixture, which is the one `feeds.py:71-72` explicitly calls out:

```python
# "generic": has a guid, but the provider regenerates it whenever an
# item is edited (typos, added tags, retitles).
```

`fixtures/snapshot-a/wire.json` has `"guid": "wire-2026-08-14-0031"`; `fixtures/snapshot-b/wire.json` has the edited version of the same item (same link, updated description) with `"guid": "wire-2026-08-14-0031-r2"`. Because `_item_key` does `raw_id or link` with no per-source branching, and `raw_id` is never empty for the wire feed, the regenerated guid is used as the key and the item is treated as new. The docstring's claim that "the generic format's `guid` is deliberately not trusted" is simply false — there is no code anywhere that special-cases the generic feed. Reproduction below under Bugs.

This is a single global rule that happens to work on 2/3 fixtures and is wrong on the specific feed the codebase's own comments flagged as the hard case — and the agent's comment claims to have solved it when it hasn't. That combination (stated intent, unmet in code) is worse than a simple oversight, so I'm scoring below the "unaddressed or unnoticed" band rather than at the top of it.

### Ambiguity handling — 2.5 / 8

Three forks exist in this codebase and none are named explicitly:

- **Per-channel vs. global suppression**: `store.seen_keys`/`filter_new` is keyed only by `source`, not by channel — one shared archive, so "seen" is global across all channels. Never discussed in README or docstring.
- **Edited item as new vs. same**: by choosing `link` as the blogroll/wire identity component, the agent silently decided edits don't count as new items. Reasonable, but never stated.
- **First-run backfill**: on an empty database every item currently in the feeds becomes "new" and gets sent on the very first run. Not mentioned anywhere — operationally relevant since this runs from cron and a first deploy could dump a full backlog.

Per the rubric's own scoring note, a global-suppression choice that is also *wrong* belongs in the 2-3 band. It is wrong here: `digest.py:40-43` records items to the archive before the channel loop even runs (see Failure-mode reasoning below), so the global/per-source archive doesn't just mean "no double-send within a run" — it means a channel that was never reached during a half-crashed run loses those items forever on the next tick. That's precisely the "global-suppression bug" the rubric names.

### Failure-mode reasoning — 2 / 8

`digest.py:32-44`:

```python
def run_once(cfg, db, dry_run=False):
    all_items = []
    for feed_cfg in cfg["feeds"]:
        ...
        fresh = store.filter_new(db, feed_cfg["name"], items)
        if not dry_run:
            store.record_items(db, feed_cfg["name"], fresh)
        all_items.extend(fresh)
    ...
    for chan_cfg in cfg["channels"]:
        ...
        channels.send(chan_cfg, body)
```

The archive insert (and its `conn.commit()`, inside `store.record_items`) happens in the **feed-fetch loop**, fully before the **channel-send loop** even starts. There is no per-channel marking at all — delivery success/failure is not tracked anywhere. `channels.py`'s own docstring says "if a channel is down we let the exception propagate and cron will pick us up again on the next tick," but by the time that exception propagates, every item from every feed has already been committed as "seen," so the next tick will not refetch them for *any* channel, including the one that crashed and the ones after it in iteration order that were never attempted.

Verified below under Bugs: a channel 2 (webhook) failure after channel 1 (stdout) succeeded loses the batch for channel 2 and for channel 3, permanently, with no discussion of this tradeoff anywhere in the code or README.

### Existing-code respect — 5.5 / 6

`feeds.py`, `channels.py`, `config.example.toml`, and `fixtures/` are untouched. The change set is surgical: `digest.py` (+2/-1 lines) and `store.py` (+34 lines, all additive). The "unconditional insert" problem the rubric calls out explicitly is dealt with — `store.record_items` is now called with `fresh` (filtered) items instead of all `items` — and `--dry-run` is preserved and actually verified to not write to the database (confirmed empirically: `digest.sqlite3` has 0 rows after a `--dry-run` invocation). No schema migration was needed and none was invented; the existing `items` table's `raw_id`/`link` columns are reused as-is. Small deduction because the *placement* of the insert (before any channel is attempted) is an architectural choice that creates the failure-mode bug above — it works with the existing schema, but not with the existing channel/delivery model.

### Code quality — 3 / 4

The new functions (`_item_key`, `seen_keys`, `filter_new`) are short, named reasonably, and use parameterized SQL (`store.py:50-52`). No vestigial columns, no dead migration code. The one real defect at the code-quality level is the `_item_key` docstring quoted above: it states a design decision ("the generic format's guid is deliberately not trusted") that the three lines of code directly beneath it do not implement. A misleading comment inside the function it describes is a maintainability hazard for whoever reads this next — it will cost them the hour it cost this review to find the real behavior.

### Documentation — 2.5 / 4

The README (`README.md:6-9`) was given real prose, not just a docstring, which avoids the rubric's 2-point docstring-only cap:

```
Only genuinely new items are ever sent: each run filters the fetched feeds
against the SQLite archive in `store.py` and sends only the unseen ones.
Dedup keys on the provider's stable id (`raw_id`) when present, else the
permalink.
```

But it covers only "what" the dedupe does, inaccurately (it doesn't mention that `raw_id` itself can rotate for the generic feed, which is the thing that's actually broken). It does not say how to reset the dedupe state (e.g. delete `digest.sqlite3` to resend everything), and does not mention first-run backfill behavior at all.

## 3. What it missed

- **The generic feed's rotating guid** — named in `feeds.py`'s own module docstring as a known trap, called out again in the agent's new docstring, and not actually handled in the code that sits two lines below that docstring.
- **Per-channel delivery tracking** — there is no notion of "channel X has/hasn't received item Y." All three ambiguity forks (per-channel vs. global suppression, edited-as-new, first-run backfill) are decided by omission rather than by design.
- **Ordering of archive-write vs. delivery** — the insert happens before the send loop starts, not after a channel succeeds, so "we recorded it" and "we delivered it" are conflated. No argument is made anywhere for at-least-once vs. at-most-once; the actual behavior that falls out is neither — it's "zero further attempts regardless of whether this run delivered anything," which is strictly worse than the bug being fixed.
- **First-run / backfill behavior** is unaddressed and undocumented — a first deploy against a long-lived feed will blast the full backlog to every channel in one digest.

## 4. Bugs

### Bug 1 — the wire/generic feed still gets re-sent on edit

Reproduction (full run, `/tmp/notify-test` is an untouched copy of the agent's tree):

```sh
$ python3 digest.py --config config.toml --db digest.sqlite3   # config points at snapshot-a
...
* Port fee inquiry opened  [wire]
  Regulator confirms inquiry.
  https://wire.example/i/0031
...
sent 12 items

$ sed -i '' 's/snapshot-a/snapshot-b/g' config.toml            # simulate the next feed poll
$ python3 digest.py --config config.toml --db digest.sqlite3
=== ops ===
...
* Port fee inquiry opened  [wire]
  Regulator confirms inquiry. Adds operator comment.
  https://wire.example/i/0031
...
sent 4 items
```

Same `link` (`https://wire.example/i/0031`), same title, re-sent because `fixtures/snapshot-b/wire.json`'s `guid` changed from `wire-2026-08-14-0031` to `wire-2026-08-14-0031-r2`. This is exactly the behavior the task asked the agent to fix ("people keep getting the same items over and over"), on exactly the feed the existing code comments warned would do this.

### Bug 2 — a channel failure mid-run permanently drops the batch for channels not yet reached

Reproduction: three channels (`ops` = stdout, `energy` = webhook to an unreachable host, `everything` = stdout), run against `snapshot-a`:

```sh
$ python3 digest.py --config config-fail.toml --db digest.sqlite3
=== ops ===
... (3 items printed successfully) ...
Traceback (most recent call last):
  ...
channels.DeliveryError: energy: <urlopen error [Errno 61] Connection refused>
$ echo $?
1
```

The process crashes before `everything` is ever reached — `everything` receives nothing. But:

```sh
$ python3 -c "import sqlite3; c=sqlite3.connect('digest.sqlite3'); print(c.execute('SELECT COUNT(*) FROM items').fetchone())"
(6,)
```

All six items from all three feeds are already in the archive — they were written in the feed-fetch loop before the channel loop ran at all. Re-running against the same config confirms the drop is permanent:

```sh
$ python3 digest.py --config config.example.toml --db digest.sqlite3 --dry-run
sent 0 items
```

`everything`, which never actually received a single item, will never receive these items on any future tick. This is a silent, permanent loss introduced by this change — arguably a worse failure mode than the duplicate-sends the task asked to fix, since duplicates are at least visible to recipients and data loss is not.

```json
{
  "identity_strategy": 5,
  "ambiguity_handling": 2.5,
  "failure_mode_reasoning": 2,
  "existing_code_respect": 5.5,
  "code_quality": 3,
  "documentation": 2.5,
  "total": 20.5,
  "would_merge": "no",
  "one_line": "Fixes 2/3 feeds; falsely claims the third works, and silently drops batches on crash."
}
```
