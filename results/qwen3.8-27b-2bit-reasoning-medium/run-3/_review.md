# Review

## 1. Summary

The agent added a per-channel `sent` table keyed on `(source, link)`, checked it before sending and wrote to it only after a successful send, and documented every choice in `store.py`'s module docstring and a new "Deduplication" README section. The identity choice explicitly reasons about the `wire` (regenerating guid) and `blogroll` (no id) feeds but never considers that the `newsroom` feed's `link` embeds a rotating `utm_campaign` query parameter — running the fixtures confirms this causes real re-sends of edited/re-tagged newsroom items, i.e. the exact failure the task is about, for one of the three feeds. I would merge this with a fix, not as-is: the mechanism and failure-mode reasoning are good, but the identity choice is unsound for the feed most likely to actually rotate identifiers.

## 2. Per-category scoring

### Identity strategy — 6/10

`store.py` docstring states the strategy clearly:

```python
Dedup identity is (source, link), NOT the feeds' own ids. The `wire` feed
regenerates guids whenever an item is edited, and `blogroll` has no stable
identifier at all; the permalink is the one field that is stable across
edits for all three feeds. Tracking is per channel (not global) because
channels' keyword filters overlap and an item matching two channels should
appear in both.
```

and `digest.py:47-54`:

```python
seen = set()
unique = []
for item in all_items:
    key = (item["source"], item["link"])
    if key in seen:
        continue
    seen.add(key)
    unique.append(item)
```

`store.unsent_items`/`mark_sent` use the same `(source, link)` key (`store.py:70-90`).

This correctly survives the `wire` guid-regeneration case (`wire-2026-08-14-0031` → `wire-2026-08-14-0031-r2`, same `link`) and the `blogroll` missing-id case. It does **not** survive the `newsroom` feed, whose `url` carries `utm_campaign=w33` in snapshot-a and `utm_campaign=w34` in snapshot-b for the *same* `entry_id`:

```
fixtures/snapshot-a/newsroom.json: "url": ".../port-fees?utm_source=feed&utm_campaign=w33"
fixtures/snapshot-b/newsroom.json: "url": ".../port-fees?utm_source=feed&utm_campaign=w34"
```

I reproduced this (see Bugs below): running against snapshot-a then snapshot-b re-sends both unchanged newsroom items to `ops`/`energy`/`everything`. `feeds.py` provides `raw_id` (the `entry_id`) precisely for this feed, and it's stable across both snapshots — the agent had the tool it needed in scope and didn't use it. This is the "single strategy that works for most items; the feed it breaks on is unaddressed or unnoticed" band — the utm_campaign case, which the task's own fixtures were built to test, is neither mentioned nor mitigated anywhere in the diff.

### Ambiguity handling — 5/8

Three forks were in play:
- **Per-channel vs global suppression**: named and resolved explicitly, correctly, in both the docstring above and the README ("Tracking is per channel, not global... an item matching two channels should appear in both"), implemented via `unsent_items(conn, channel, items)` (`store.py:70`).
- **Edited item as new vs same**: named explicitly (the guid/permalink discussion above is exactly this fork), but the resolution is buggy for `newsroom` as shown above — a named decision that's wrong for one of the three feeds it needs to cover.
- **First-run backfill**: not named anywhere. Nothing in code or docs says what happens the first time `digest.sqlite3` doesn't exist yet — in practice everything currently in the feeds gets sent once, which is a defensible default, but it's an undiscussed silent choice, and on a channel with a huge feed history it could be a surprise.

Two of three forks are named; one of the two named ones has a real bug, and the third isn't discussed at all. That sits below "all three forks identified and resolved explicitly" but above the fully-silent bands.

### Failure-mode reasoning — 7/8

`store.mark_sent` is called strictly after `channels.send` succeeds, per channel, inside the channel loop (`digest.py:70-71`):

```python
channels.send(chan_cfg, body)
store.mark_sent(db, chan_cfg["name"], selected)
sent += len(selected)
```

with the docstring on `mark_sent` naming the tradeoff:

```python
def mark_sent(conn, channel, items):
    """Record `items` as delivered to `channel`.

    Called only after a successful send: if the channel is down the
    exception propagates and the next cron tick retries them.
    """
```

`channels.py`'s own docstring confirms delivery is "best effort" and exceptions propagate. I traced the multi-channel scenario: if channel 2 of 3 raises, channel 1's items are already committed as sent (`mark_sent` calls `conn.commit()` immediately), channel 2/3's are not, and the whole `run_once` call raises out of `main()`, uncaught — so the process exits non-zero and cron's next tick retries the un-marked channels. That's correct at-least-once behavior with no double-loss, and the ordering/tradeoff is argued in prose, not just implemented. Docked half a point because there's no top-level handling to let channel 3 still get a chance in the *same* run if channel 2 fails (it just crashes the whole run), which is a minor availability cost the docs don't call out.

### Existing-code respect — 6/6

`feeds.py`, `render.py`, and `channels.py` are untouched. `--dry-run` still short-circuits before `mark_sent`, with a comment explaining why (`digest.py:65-68`: "Don't mark as sent: a preview must not consume items"). The pre-existing unconditional `store.record_items` archive-insert (present in the original `digest.py` before this change) was left as-is rather than rewritten — reasonable, since the rubric treats that as acceptable if not newly broken, and it wasn't touched. Schema change is additive (`CREATE TABLE IF NOT EXISTS sent`, `store.py:34`), so an existing `digest.sqlite3` upgrades cleanly with no migration script needed.

### Code quality — 3.5/4

The new SQL is simple and correct (`INSERT OR IGNORE` for idempotent marking, composite primary key doubling as the lookup index). No dead code, no vestigial columns. Minor quibble: `unsent_items` (`store.py:70-76`) pulls the *entire* sent-set for a channel into a Python set on every call rather than doing the filtering in SQL (e.g. a single `NOT IN` join per item), which is fine at fixture scale but would be worth a comment about expected table size given `prune_sent` is the only thing bounding it.

### Documentation — 3/4

The README's new "Deduplication" section (`README.md`) covers identity choice, per-channel tracking, mark-after-send-with-retry, and 30-day pruning, each with the "why" stated — well above a docstring-only writeup. It stops short of the full rubric ask: it never says what happens on first run, and never gives the operator a concrete "how to force a resend" recipe (e.g. `DELETE FROM sent WHERE channel = 'ops'` or delete the db) — it only warns in the abstract that changing identity "becomes wrong; you will need to migrate it" without saying how.

## 3. What it missed

- The `newsroom` feed's `raw_id` (`entry_id`) is stable and available exactly for this purpose, but the agent chose `link` uniformly across feeds without checking whether `link` itself was stable per feed — it wasn't, for `newsroom`.
- First-run backfill flood is undiscussed: nothing prevents (or warns about) the entire current-feed contents being sent the first time the script runs against a fresh db.
- No test was added or run against the two fixture snapshots to validate the fix — which is precisely the check the task setup makes trivial to do (`fixtures/snapshot-a` → `fixtures/snapshot-b`).
- `prune_sent`'s 30-day window is a reasonable default but is not configurable and not mentioned in `config.example.toml`; an operator wanting a different retention has no obvious knob.

## 4. Bugs

**Re-send of edited `newsroom` items across snapshots**, reproduced against a scratch copy at `/tmp/notify-digest-test`:

```
$ cp config.example.toml config.toml   # points feeds at snapshot-a
$ python3 digest.py --config config.toml --db digest.sqlite3
sent 12 items          # first run, everything goes out, as expected

$ python3 digest.py --config config.toml --db digest.sqlite3
sent 0 items            # second run, same snapshot: correctly deduped

$ sed -i '' 's/snapshot-a/snapshot-b/g' config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3
=== ops ===
Ops digest — 2 item(s)
* Union responds to port fee inquiry  [newsroom]      <- genuinely new, fine
* Regulator opens inquiry into port fees  [newsroom]   <- BUG: same entry_id
  https://newsroom.example/2026/08/port-fees?utm_source=feed&utm_campaign=w34
                                                        (was ...&utm_campaign=w33 in snapshot-a)
=== energy ===
Energy digest — 1 item(s)
* Grid operator delays offshore tender  [newsroom]     <- BUG: same entry_id, resent
sent 6 items
```

`entry_id` 84121 ("Regulator opens inquiry into port fees") and 84118 ("Grid operator delays offshore tender") were already sent in run 1 and are unedited in substance between snapshots — only their tracking query string rotated (`utm_campaign=w33` → `w34`) — yet both are re-sent to `ops`/`energy`/`everything` on the very next simulated cron tick. This is the same symptom the task complains about ("people keep getting the same items over and over"), just narrowed to one feed, and it is directly caused by using `link` as the newsroom identity instead of the `raw_id`/`entry_id` that `feeds.py` already extracts for that format.

By contrast, the `wire` edited item (`guid` changed, `link` unchanged) and the `blogroll` edited item (title/excerpt changed, `permalink` unchanged) were correctly *not* re-sent in the same run — confirming the fix works for the two cases the agent explicitly reasoned about, and fails for the one it didn't.

```json
{
  "identity_strategy": 6,
  "ambiguity_handling": 5,
  "failure_mode_reasoning": 7,
  "existing_code_respect": 6,
  "code_quality": 3.5,
  "documentation": 3,
  "total": 30.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid per-channel dedupe, but link-based identity re-sends utm_campaign-rotated newsroom items."
}
```
