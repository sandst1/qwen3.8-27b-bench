# Review: notify-digest dedupe fix

## 1. Summary

The agent added a seen-items check (`store.unseen_items`) keyed on `(source, raw_id or link)`, gated `record_items` behind `--dry-run`, and wrote up the dedupe behaviour in the README and module docstrings. It correctly stops the newsroom and blogroll feeds from repeating, but it trusts the `wire`/`generic` feed's `guid` even though `feeds.py` documents — in a comment the agent read — that this exact provider regenerates the guid on every edit, and I reproduced the resulting duplicate send below. The marking also happens before any channel delivery is attempted, so a mid-run channel failure silently and permanently drops items for every channel instead of retrying them on the next cron tick. I would not merge this as-is: the headline complaint is only two-thirds fixed and the fix introduces a new, undocumented data-loss mode.

## 2. Per-category scoring

### Identity strategy — 6/10

The key function is `store.py:34-38`:

```python
def item_key(source, item):
    """Identity of an item within a source: the feed's raw id when it has
    one, else the permalink. Blogroll posts have no raw id, so they key on
    their link instead."""
    return source, item.get("raw_id") or item["link"]
```

This correctly handles two of the three traps the fixtures are built to exercise:
- **Rotating `utm_campaign`** (newsroom): handled, because `raw_id` (the `entry_id`) is preferred over the link, and the link's query string is what rotates.
- **Missing `raw_id`** (blogroll): handled, falls back to `link`/permalink, which is stable across the edit in `fixtures/snapshot-b/blogroll.json` (title and excerpt change, permalink doesn't).
- **Regenerated `guid`** (wire/"generic"): **not** handled. `feeds.py:71-72` spells this out:

  ```python
  # "generic": has a guid, but the provider regenerates it whenever an
  # item is edited (typos, added tags, retitles).
  ```

  Because `item_key` prefers `raw_id` whenever it is truthy, and wire's `guid` is truthy even after it changes, the wire item is treated as brand-new when its guid rotates. Reproduction:

  ```
  snapshot-a wire: {"guid": "wire-2026-08-14-0031",    "link": ".../i/0031", "title": "Port fee inquiry opened"}
  snapshot-b wire: {"guid": "wire-2026-08-14-0031-r2", "link": ".../i/0031", "title": "Port fee inquiry opened"}
  ```

  Running the digest against snapshot-a, then snapshot-b, with the same database:

  ```
  $ python3 digest.py --config config.toml --db digest.sqlite3   # config points at snapshot-a
  ...
  * Port fee inquiry opened  [wire]
    https://wire.example/i/0031
  sent 12 items

  $ sed -i 's/snapshot-a/snapshot-b/' config.toml
  $ python3 digest.py --config config.toml --db digest.sqlite3
  === ops ===
  Ops digest — 2 item(s)
  * Union responds to port fee inquiry  [newsroom]
  * Port fee inquiry opened  [wire]        <-- same link, resent
    https://wire.example/i/0031
  sent 4 items
  ```

  The exact symptom in the bug report — "people keep getting the same items over and over" — still happens, for the one feed format that was explicitly documented as having this failure mode. This is a single strategy that works for most items, with the feed it breaks on unaddressed (not even mentioned in the README's "How it works" section). That matches the rubric's 6–8 band; I put it at the bottom of that band because the breakage is on the one feed the codebase comments specifically warn about, and because the agent's own verification run (see `_agent.log`, probe3, timestamp `2026-10-22T11:48:29`) printed this exact duplicate and it was not flagged or fixed.

### Ambiguity handling — 2/8

Three forks exist in this codebase and none are named in prose:

- **Per-channel vs. global suppression** — decided silently, and wrong. Marking happens once per feed in `digest.py:44-47`, before the per-channel send loop even starts, so "seen" is a single global flag shared by every channel:

  ```python
  new_items = store.unseen_items(db, feed_cfg["name"], items)
  if not dry_run:
      store.record_items(db, feed_cfg["name"], new_items)
  all_items.extend(new_items)
  ...
  for chan_cfg in cfg["channels"]:
      ...
      channels.send(chan_cfg, body)
  ```

  I reproduced the consequence of this (see Bugs, below): if channel 2 fails, channel 3's never-attempted items are already marked seen and are lost forever. This is precisely the "global-suppression bug" the rubric's scoring notes call out as a 2-point ambiguity failure, and it is not mentioned anywhere in the README or docstrings.
- **Edited item as new vs. same** — decided silently; correct for blogroll and newsroom, wrong for wire (see Identity strategy above), and never stated as a decision at all.
- **First-run backfill** — decided silently (everything in the archive-less DB is "new" and gets sent once, which is a reasonable default), but never stated. On a fresh install, cron will deliver whatever backlog is in the feeds at that moment with no warning in the README that this will happen.

All three forks are real in this codebase and all three are undiscussed; one of them is actively wrong. That lands at the bottom of the "decided silently, and at least one decision is wrong (typically global suppression)" band (2–3).

### Failure-mode reasoning — 2.5/8

`channels.py`'s own docstring states the intended contract: *"Delivery is best effort: if a channel is down we let the exception propagate and cron will pick us up again on the next tick."* That implies retry-until-delivered semantics. The agent's change breaks that contract without noticing, because the seen-marker is written before any channel is attempted, per `digest.py:44-47` (quoted above) — not per-channel, not after a successful send.

Reproduction — a feed with two channels, the second of which is unreachable:

```
$ python3 digest.py --config config_fail.toml --db digest.sqlite3
=== ops ===                      # channel 1 succeeds
Ops digest — 1 item(s)
* Regulator opens inquiry into port fees  [newsroom]
...
channels.DeliveryError: broken: <urlopen error [Errno 61] Connection refused>   # channel 2 fails, exception propagates
$ exit code 1

$ sqlite3 digest.sqlite3 "select source, raw_id, title from items"
newsroom|84121|Regulator opens inquiry into port fees
newsroom|84118|Grid operator delays offshore tender     # <- never reached "broken", already archived

$ # now "fix" the broken channel and rerun
$ python3 digest.py --config config_fail2.toml --db digest.sqlite3
sent 0 items        # <- "Grid operator delays offshore tender" is gone for good
```

Both items — including the one that never reached the failing channel at all — are permanently marked seen and will never be offered to that channel again, even after the channel recovers. This is a partial-failure window that silently drops a whole batch, which the rubric places at 2–3 points, and there is zero discussion of the tradeoff anywhere in the diff. I give it 2.5 rather than the floor because marking is at least ordered sensibly relative to the feed-fetch (it won't re-process a feed that failed to fetch), just not relative to delivery.

### Existing-code respect — 5.5/6

The diff is minimal and additive: `feeds.py`, `channels.py`, and the schema are untouched; `digest.py` and `store.py` get small, in-place extensions (`git diff --stat`: 3 files, well under 40 lines changed). `--dry-run` is preserved and, in fact, its one latent bug is fixed — originally `store.record_items` ran unconditionally even under `--dry-run` (`store.record_items(db, feed_cfg["name"], items)` with no gate in the pre-change `digest.py`); the agent's `if not dry_run:` guard at `digest.py:45` is the correct fix for that, and it's a real instance of "the unconditional insert is dealt with" per the rubric's existing-code-respect note. No schema migration was needed because no columns changed. Docking half a point because the new per-item `SELECT` in `unseen_items` runs against the unbounded, ever-growing `items` archive with no supporting index (see Code quality) — a minor but foreseeable cost on code that's explicitly a permanent archive.

### Code quality — 3/4

Readable and consistent with the existing style, with docstrings on both new functions. Two nits:
- `item_key` returns `(source, ...)` even though `source` is already known by every caller (`store.py:45`: `key_source, key_value = item_key(source, item)` — `key_source` is always just `source` echoed back), which is vestigial indirection.
- `unseen_items` issues one `SELECT` per item (`store.py:46-49`), scanning `COALESCE(raw_id, link)` with no supporting index — `idx_items_source` only covers `source`. Fine at fixture scale, but the `items` table is explicitly a permanent, unbounded archive (`store.py:3-5`), and this is now a hot path executed every 15 minutes, so it will get slower as history grows.

### Documentation — 2.5/4

The README gained a real "How it works" section (`README.md:17-27`), not just a docstring, so it clears the 2-point docstring cap:

```markdown
## How it works

Each run fetches the feeds, asks the SQLite archive (`store.py`) which items
are new, and only dispatches the new ones to the channels.

An item counts as already-seen when the archive holds a row with the same
source and the same feed id — the feed's `raw_id` when it has one, else the
permalink (blogroll posts have no raw id). Unchanged feeds therefore produce
no digests on later cron ticks; only items with a new feed id get through.

`--dry-run` previews without touching the archive.
```

It explains the dedupe mechanism and correctly flags the blogroll fallback case. It does **not** mention: how to reset the dedupe state (delete/truncate `digest.sqlite3`?), what happens on the very first run (full backlog gets sent once, unannounced), the wire/guid caveat that's already documented one file over in `feeds.py`, or anything about channel-failure/retry semantics. Two of the rubric's three named questions (reset, first-run) are unaddressed.

## 3. What it missed

- The `generic`/wire feed's documented guid-regeneration behaviour — the agent's own test run showed the resulting duplicate and it went unaddressed.
- Per-channel vs. global suppression was never named as a decision; the implementation picked global, and it's the wrong pick given `channels.py`'s stated best-effort/retry contract.
- No discussion of what happens when a channel fails partway through a run — not in code comments, not in the README, not in commit messages (there are none; this wasn't committed).
- First-run backfill behavior (send everything once if the archive is empty) is undocumented.
- No "how to reset" story — operators have no guidance on what deleting or truncating `digest.sqlite3` does to behavior, despite the table now being load-bearing for dedupe rather than just an archive.
- No index to support the new per-item lookup against a table that's explicitly designed to grow forever.

## 4. Bugs

**Bug 1 — duplicate delivery on the `generic`/wire feed when its `guid` rotates.** Identity strategy prefers `raw_id`, and `feeds.py` documents that this provider's `guid` is regenerated on edit. Repro (same as Identity strategy section):
```
1. Point config at fixtures/snapshot-a, run once. "Port fee inquiry opened" [wire] is sent.
2. Point config at fixtures/snapshot-b (guid changed from wire-2026-08-14-0031 to
   wire-2026-08-14-0031-r2, link unchanged), run again against the same --db.
3. "Port fee inquiry opened" [wire] is sent a second time.
```
This is the exact complaint in the task prompt, still present for one of three feed types.

**Bug 2 — permanent item loss on partial channel failure.** The seen-marker is written per-feed before any channel delivery is attempted (`digest.py:44-47`), not per-channel and not after a successful send. Repro:
```
1. Config with one feed, two channels; second channel is a webhook that's unreachable.
2. Run digest.py: channel 1 gets its items and succeeds, channel 2 raises DeliveryError,
   the exception propagates and the process exits 1.
3. Inspect digest.sqlite3: the items for BOTH channels are already in the `items` table,
   including items that never even reached channel 2 because the loop hadn't gotten there.
4. Fix the webhook and rerun: "sent 0 items" — those items are never offered to any
   channel again.
```
This contradicts `channels.py`'s own stated contract ("cron will pick us up again on the next tick") and silently discards data on infrastructure blips, which is arguably worse than the original "sends everything every time" behavior for anyone relying on at-least-once delivery.

```json
{
  "identity_strategy": 6,
  "ambiguity_handling": 2,
  "failure_mode_reasoning": 2.5,
  "existing_code_respect": 5.5,
  "code_quality": 3,
  "documentation": 2.5,
  "total": 21.5,
  "would_merge": "no",
  "one_line": "Fixes 2 of 3 feeds' dedupe but adds a silent item-loss-on-failure bug."
}
```
