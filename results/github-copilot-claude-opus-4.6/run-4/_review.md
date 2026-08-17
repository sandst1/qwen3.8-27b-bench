# Review

## 1. Summary

The agent added a `sent_items(raw_id, channel)` table and gated sending on `store.is_sent()`/`store.mark_sent()`, which is the right shape of fix and a minimally invasive diff (only `digest.py` and `store.py` touched). It never noticed that one of the three fixtures (`blogroll`) has `raw_id = None`, so those items silently never get recorded as sent and are re-sent on every single cron tick forever — the exact bug being reported, still present for a third of the feeds. No README update and no discussion of the ambiguity forks (edited-item-as-new, per-channel vs. global, first-run backfill) accompanies the change, despite the prompt explicitly asking for that. I would not merge this as-is; it needs the blogroll case fixed and the decisions written down before it goes near cron.

## 2. Per-category scoring

### Identity strategy — 4/10

The whole strategy is `item["raw_id"]` used verbatim as the dedupe key, per channel:

```python
# digest.py:49
new = [i for i in selected if not store.is_sent(db, i["raw_id"], chan_cfg["name"])]
```

`feeds.py` already documents, in a comment the agent left untouched, that this is unsafe for two of the three formats:

```python
# feeds.py:57-58
if fmt == "blogroll":
    # No stable identifier of any kind in this one.
    ...
    "raw_id": None,
```
```python
# feeds.py:71-72
# "generic": has a guid, but the provider regenerates it whenever an
# item is edited (typos, added tags, retitles).
```

I ran both fixtures through the agent's code (scratch copy in `/tmp/nd-test2`, run against `snapshot-a` then `snapshot-b`, simulating two cron ticks 15 minutes apart):

- `newsroom` (`entry_id`) works correctly — the item whose URL only gained a rotated `utm_campaign` was correctly suppressed; only the genuinely new "Union responds..." item was sent on the second run.
- `wire`/generic feed: the edited item's `guid` rotated (`wire-2026-08-14-0031` → `wire-2026-08-14-0031-r2`) and was resent as if brand new on the second run — exactly the documented failure mode, unaddressed.
- `blogroll`: every item has `raw_id = None`. Querying `sent_items` after two full runs on the *same* snapshot shows zero blogroll rows were ever recorded:

```
$ sqlite3 digest.sqlite3 "select * from sent_items"
wire-2026-08-14-0031|ops|...
84121|ops|...
84118|energy|...
wire-2026-08-13-0918|energy|...
...   (no blogroll raw_id ever appears)
```

Running `digest.py` twice in a row against the unmodified `snapshot-a` reproduces the user's complaint verbatim for this feed — the "Ops digest" on run 2 still contains "Notes on port fee arithmetic [blogroll]" that was already delivered on run 1. This is a naive single-strategy dedupe that breaks on two of the three fixture feeds — squarely the 3–5 band, scored at the low end because one of the breakages (blogroll) is a silent no-op forever, not just a one-off.

### Ambiguity handling — 3/8

- Per-channel vs. global: decided correctly (`sent_items` keyed on `(raw_id, channel)`), and named, but only in a one-line code comment (`digest.py:48`) — never surfaced in the README.
- Edited-item-as-new-vs-same: decided silently, by construction (raw `guid`/`entry_id` equality), and for the `generic` feed it's decided *wrong* relative to the feed's own documented behavior (see above).
- First-run backfill: not mentioned anywhere. On a fresh `digest.sqlite3`, the entire fixture history is sent as "new" on the very first invocation — a reasonable default, but nowhere stated as a choice, and nowhere is there guidance for a human doing the first deploy who will otherwise be surprised by the flood.

Two of three forks are silent, and one of them (edited-as-new for `generic`) is wrong on the fixture the code's own comment warns about.

### Failure-mode reasoning — 5/8

Marking happens after delivery and is scoped per channel:

```python
# digest.py:56-58
channels.send(chan_cfg, body)
store.mark_sent(db, [i["raw_id"] for i in new], chan_cfg["name"])
sent += len(new)
```

This is the correct ordering for the at-least-once model that `channels.py` already documents ("Delivery is best effort: if a channel is down we let the exception propagate and cron will pick us up again on the next tick" — `channels.py:4-5`, pre-existing, untouched). If channel 2 raises, channel 1's items stay marked sent (no duplicate there) and channel 2's batch is simply retried whole on the next tick (no partial loss). That's sane and matches the pre-existing crash-and-retry contract. It scores mid-band rather than top because none of this is argued anywhere — the interaction between `mark_sent`'s placement and the existing "let it propagate" comment is never discussed in code or README, and the "whole batch re-sent if `mark_sent` never runs" tradeoff (duplicates on crash-after-send) is left implicit.

### Existing-code respect — 5.5/6

The diff is surgical: `feeds.py`, `channels.py`, `render.py` are untouched. `--dry-run` still short-circuits before `mark_sent` is called (`digest.py:53-56`), so dry runs correctly don't affect dedupe state. The `items` archive table is left as pure archive, exactly as before. Schema evolution for an already-deployed `digest.sqlite3` is handled for free via `CREATE TABLE IF NOT EXISTS sent_items` (`store.py:24`), so existing databases upgrade in place without a migration script. Docked half a point only because the fix leaves a known, commented-on feed format (`blogroll`) functionally undeduped, which a more careful pass over the existing code (the comment is right there at `feeds.py:58`) would have caught.

### Code quality — 2.5/4

Clean, small functions, no dead code, sensible naming. Two real quality problems:

1. `is_sent` issues one query per item (`digest.py:49` inside a list comprehension calling `store.is_sent` per item) — an N+1 pattern; fine at fixture scale, would not scale well.
2. Silent NULL-handling bug: `store.is_sent` does `WHERE raw_id = ? AND channel = ?` with `raw_id=None` bound as SQL `NULL`; `NULL = NULL` is false in SQLite, so this always returns "not sent" for any item lacking a `raw_id`, with no guard, comment, or fallback anywhere in `is_sent` or `mark_sent`:

```python
# store.py:57-63
def is_sent(conn, raw_id, channel):
    """Return True if this item was already sent to this channel."""
    row = conn.execute(
        "SELECT 1 FROM sent_items WHERE raw_id = ? AND channel = ?",
        (raw_id, channel),
    ).fetchone()
    return row is not None
```

Meanwhile `sent_items.raw_id` is declared `NOT NULL` (`store.py:25`), so `mark_sent`'s `INSERT OR IGNORE` with `raw_id=None` silently fails the constraint and inserts nothing — a second, compounding silent failure with no comment explaining (or even acknowledging) it.

### Documentation — 1.5/4

`README.md` is byte-for-byte identical to the original (`diff` confirms zero changes) — no mention of dedupe, no reset instructions, no first-run behavior, nothing about the blogroll gap. The only documentation is a module docstring update in `store.py`:

```python
# store.py:1-6
"""SQLite persistence.

The `items` table is an archive of everything we have ever seen.
The `sent_items` table tracks which (raw_id, channel) pairs have already been
dispatched, so that cron re-runs never send duplicates.
"""
```

That claim ("never send duplicates") is actually false for the `blogroll` feed, demonstrated above — so what little documentation exists is also misleading. Per the rubric, a docstring alone caps this category at 2; it's scored slightly under that because the docstring's central claim doesn't hold.

## 3. What it missed

- The `blogroll` format's `raw_id: None` is the most visible ambiguity in the entire codebase (it's called out in a comment right next to the field), and the fix doesn't handle it — no fallback to `link`/`title`+`published` hash, no warning log, nothing.
- No decision recorded anywhere about what "same item" means when an editor changes an article (generic feed's rotating `guid`) — the fix just inherits whatever the upstream feed calls stable, silently.
- No mention of how to reset/clear the dedupe state (e.g., "drop `sent_items` to resend everything") for the next operator who needs to force a re-send.
- No mention of first-run behavior (the entire fixture backlog goes out at once) which is exactly the kind of surprise a 3am on-call engineer would want flagged.
- No test added despite there being an obvious repeatable scenario (run `digest.py` twice against the same fixture) that would have caught the blogroll regression immediately.

## 4. Bugs

**Blogroll items are never deduplicated and are re-sent on every cron tick, forever.** Reproduction:

```sh
cp -R <agent-worktree> /tmp/nd-test
cd /tmp/nd-test
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3   # run 1: sends 12 items
python3 digest.py --config config.toml --db digest.sqlite3   # run 2, same snapshot:
```

Run 2 output includes, unchanged from run 1, without any new feed content:

```
=== ops ===
Ops digest — 1 item(s)
* Notes on port fee arithmetic  [blogroll]
  Working through the levy numbers.
  https://blog.example/port-fee-arithmetic
```

This is the reported symptom, unresolved for one of the three feeds, and would repeat every 15 minutes indefinitely in production since `blogroll` items never accumulate in `sent_items` (verified directly against the sqlite file — zero blogroll rows present after multiple runs).

```json
{
  "identity_strategy": 4,
  "ambiguity_handling": 3,
  "failure_mode_reasoning": 5,
  "existing_code_respect": 5.5,
  "code_quality": 2.5,
  "documentation": 1.5,
  "total": 21.5,
  "would_merge": "no",
  "one_line": "Fixes 2 of 3 feeds; blogroll items still re-sent forever, undocumented."
}
```
