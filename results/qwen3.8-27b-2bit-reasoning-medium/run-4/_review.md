# Review

## 1. Summary

The agent added a `dedup_key` column to `items` (the item's link, stripped of query/fragment), made `record_items` check-then-insert against `(source, dedup_key)` and return only the new items, wired that through `digest.run_once`, added an idempotent migration for existing databases, and wrote a substantial README section explaining the identity choice and its known failure modes. The dedup logic itself is well-reasoned and verified against all three fixture quirks (rotating `utm_*`, regenerated `guid`, no-id blogroll), but the mark-as-seen step happens for every fetched item before any channel has actually received it, so a channel outage or a mid-run crash silently and permanently drops items for that channel — a failure mode the write-up never mentions. I would merge this with a required follow-up fix for that gap, not as-is for a system where channel delivery genuinely fails sometimes (which is the stated reason `channels.py`'s `webhook` path exists).

## 2. Per-category scoring

### Identity strategy — 9/10 (max 10)

`store.py:53-64`:

```python
def item_key(item):
    """Stable identity for an item: its link, normalised (see module doc)."""
    link = item.get("link") or ""
    if link:
        return _normalize_link(link)
    # Defensive: the normalised shape always carries a link, but if one is
    # ever missing, fall back to whatever else identifies the item.
    return (item.get("raw_id") or item.get("title") or "").strip()
```

with `_normalize_link` stripping query and fragment (`store.py:47-51`). I reproduced all three feed quirks by diffing `fixtures/snapshot-a` vs `fixtures/snapshot-b` and running `digest.py` against each in sequence:

- `wire`'s `guid` changes from `wire-2026-08-14-0031` to `wire-2026-08-14-0031-r2` on an edit — not re-sent, because identity is the link, not the guid.
- `newsroom` rotates `utm_campaign` from `w33` to `w34` on every item, including unchanged ones — not re-sent, because the query string is stripped.
- `blogroll` has no id at all, only a `permalink` — handled since identity is the link itself.

Second run on snapshot-b only produced the one genuinely new item (`Union responds to port fee inquiry`), confirmed via:

```
=== RUN B (should only show genuinely new items) ===
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
...
sent 2 items
```

The failure mode is stated explicitly in both the module docstring and the README ("If a feed ever reuses one link for genuinely different content, that content will be collapsed into one item... change `item_key`"). This is the "9-10" band: a strategy that survives all three fixtures with the tradeoff named. Docked half a point because the fallback path (`raw_id` or `title` when link is missing) is untested against any fixture and is a plain string match with no normalization of its own — a minor, acknowledged-as-defensive gap, not a real one given the current feeds.

### Ambiguity handling — 3/8 (max 8)

Three forks exist: edited-item-as-new-or-same, per-channel-vs-global suppression, and first-run backfill.

**Edited item — named and resolved explicitly**, in both the docstring (`store.py:20-22`) and README:

> An item that is *edited* (new guid / new title, same link) is the same item and is not re-sent.

**First-run backfill — decided silently but correctly.** Nothing in the code or docs states what happens on an empty database, but the natural behavior (empty table → everything looks new → whole current feed content goes out once) is reasonable and I verified it: `sent 12 items` on the very first run against an empty `digest.sqlite3`.

**Per-channel vs. global suppression — decided silently, and wrong.** `digest.py:34-43` marks an item "sent" the moment it is fetched, in a loop that runs entirely before any channel is touched:

```python
for feed_cfg in cfg["feeds"]:
    ...
    new_items = store.record_items(db, feed_cfg["name"], items, dry_run=dry_run)
    all_items.extend(new_items)
...
for chan_cfg in cfg["channels"]:
    ...
    channels.send(chan_cfg, body)
```

There is no per-channel state anywhere — `items` has one row per `(source, dedup_key)`, full stop. I simulated one channel ("energy") raising `DeliveryError` mid-run:

```
crashed: simulated webhook outage
=== second run ===
sent 0 items
```

The items that "energy" never received are gone for good on the next run, because `record_items` already committed them as seen before `channels.send` was ever called for any channel. This is exactly the rubric's "typically global suppression" failure and it is undocumented — neither the docstring nor the README mentions that channel delivery failure interacts with the dedup table at all. This lands the category at 2-3 despite the one correctly-named fork, per the rubric's explicit note that a wrong silent decision caps the score there.

### Failure-mode reasoning — 2/8 (max 8)

Same defect as above, scored under its own lens: is delivery crash/retry semantics considered at all? Marking happens once, globally, at fetch time — not per channel, and not after successful delivery. `channels.py`'s original docstring says "Delivery is best effort: if a channel is down we let the exception propagate and cron will pick us up again on the next tick" — but the agent's change makes that comment false: on the next tick, `record_items` has already suppressed the very items that failed to deliver. The reproduction above (`sent 0 items` on retry after a simulated `energy` outage) demonstrates a partial-failure window that silently drops a whole batch for the failed channel, with no argued tradeoff anywhere in the diff, docstring, or README. This matches the rubric's 2-3 band ("a partial-failure window that loses or duplicates a whole batch"); I put it at the low end because there is literally zero written acknowledgement that channels can fail independently of the dedup decision.

### Existing-code respect — 6/6 (max 6)

The agent reused the existing `items` table rather than adding a parallel one, and specifically dealt with the unconditional-insert problem the rubric calls out — the old `record_items` (`store.py` original) did a bare `executemany` insert every run with no identity check at all; the new version does a `SELECT` before `INSERT` per item (`store.py:100-124`). `feeds.py`, `channels.py`, and `render.py` are untouched. `--dry-run` was preserved and actually improved (it no longer risks consuming items — see below), which I verified directly rather than taking on faith:

```
=== dry-run A ===
...
sent 0 items
=== real run A after dry-run (should still show items, not 0) ===
...
sent 12 items
```

No scope creep, no rewritten modules, no broken CLI surface.

### Code quality — 3.5/4 (max 4)

`_migrate` (`store.py:78-93`) is a clean, idempotent, well-commented migration that I verified against a hand-built "old-schema" database with duplicate rows from the pre-fix `record_items` behavior — it collapsed the duplicate to one row and kept the earliest `first_seen`:

```
{'id': 1, 'source': 'wire', ..., 'first_seen': '2026-08-17 07:06:59', 'dedup_key': 'https://wire.example/i/0031'}
count: 1
```

No dead code, no vestigial columns (raw_id is still legitimately stored data, just no longer used for identity). Minor ding: `record_items` does a per-item `SELECT` then a separate `INSERT` (`store.py:108-124`) inside a Python loop rather than relying on the new unique index with `INSERT OR IGNORE`, which would be shorter and equally correct given the index it already creates. Not wrong, just not as tight as it could be.

### Documentation — 3/4 (max 4)

The README gained a full "## Deduplication" section (`README.md:31-63`) explaining the identity choice, its consequences, the migration, and dry-run's interaction with it — well above the "docstring alone" cap. It does not, however, cover two things the rubric explicitly asks for: how to reset the dedup state (e.g., what to delete/run to re-send everything) is never mentioned, and "what happens on first run" is left implicit rather than stated (I had to run it to confirm the DB-empty behavior myself). It also never surfaces the per-channel/global-suppression decision or the failure-mode gap found above, which is the more consequential omission.

## 3. What it missed

- **Per-channel vs. global suppression** was never named as a decision. The implementation picked "global, before delivery" — the worst combination, since it suppresses future retries for channels that never actually got the item.
- **Crash/retry semantics** are entirely unconsidered. There's no per-channel sent-marker, no attempt to mark only after `channels.send` succeeds, and no argument for at-least-once vs at-most-once.
- **How to reset dedup state** (e.g., "delete `digest.sqlite3`" or "run `DELETE FROM items`") is not documented anywhere.
- **First-run behavior** is not explicitly stated, only implied by the empty-table default.
- The defensive fallback in `item_key` (missing link → `raw_id` or `title`) is untested against any fixture and not discussed as a fork in its own right (none of the three feeds currently lack a link, so it's dead in practice but still shipped).

## 4. Bugs

**Confirmed: partial channel failure permanently drops items for the failed channel.**

Reproduction (run from a fresh `digest.sqlite3`, config with `ops`/`energy`/`everything` channels against `snapshot-a`):

```python
import store, digest, channels
cfg = digest.load_config("config-a.toml")
db = store.connect("digest.sqlite3")
orig_send = channels.send
def failing_send(chan_cfg, body):
    if chan_cfg["name"] == "energy":
        raise channels.DeliveryError("simulated webhook outage")
    return orig_send(chan_cfg, body)
channels.send = failing_send
digest.run_once(cfg, db)   # raises DeliveryError partway through "energy"
```

Output:
```
=== ops ===          # ops delivered fine
Ops digest — 3 item(s)
...
crashed: simulated webhook outage
```

Then running the CLI again against the same DB:
```
$ python3 digest.py --config config-a.toml --db digest.sqlite3
sent 0 items
```

The three items destined for `energy` (`Grid operator delays offshore tender`, `Tender timetable under review`, `A short history of offshore tenders`) are gone forever — `record_items` had already committed them as "seen" in the fetch loop (`digest.py:34-43`), before `channels.send` was ever invoked for `energy`. This directly contradicts `channels.py`'s own docstring claim that "cron will pick us up again on the next tick" for a down channel.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 3,
  "failure_mode_reasoning": 2,
  "existing_code_respect": 6,
  "code_quality": 3.5,
  "documentation": 3,
  "total": 26.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid link-based dedup, verified working, but loses items on channel failure."
}
```
