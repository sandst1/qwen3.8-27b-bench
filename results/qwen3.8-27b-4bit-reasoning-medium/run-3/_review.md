# Review — notify-digest, run-3

## 1. Summary

The agent added a `link_key` (scheme+host+path, query/fragment dropped) as the dedup identity in `store.py`, wired `digest.py` to only send/record genuinely-unseen items, wrote a real migration for existing databases, and documented the whole thing in a new "How dedup works" README section that correctly names all three feeds' identifier problems. The identity strategy itself is well-chosen and verified to work against both fixture snapshots. However, the agent moved the "mark as seen" step to happen *before* any channel delivery is even attempted, which reintroduces a data-loss bug on partial channel failure that the original codebase's own `channels.py` docstring explicitly promises won't happen ("cron will pick us up again on the next tick") — I reproduced this below. I would merge this with fixes: the identity work is good and should be kept, but the marking-before-delivery ordering must be fixed before it goes near production cron.

## 2. Per-category scoring

### Identity strategy — 9 / 10

`store.py:48-57`:
```python
def _link_key(link):
    """Canonical form of an item link; the stable identity used for dedup.
    ...
    """
    parts = urlsplit(link or "")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))
```
keyed with `(source, link_key)` via a unique index (`store.py:96-98`). The docstring at the top of `store.py:12-25` explicitly names the failure mode per feed:

```
  * newsroom  -> stable `entry_id`, but its URLs carry rotating tracking
                 params (utm_campaign=w33, w34, ...) that change each poll.
  * blogroll  -> no identifier at all; the permalink is the only stable field.
  * wire      -> has a `guid`, but it is regenerated whenever an item is
                 edited (a typo fix or retile changes it).
```

I ran this against the fixtures to confirm it actually holds up, not just reads well:

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # same snapshot again
sent 0 items
$ # point config at snapshot-b (utm_campaign rotates w33->w34, wire guid gets
$ # a "-r2" suffix + edited body, blogroll permalink unchanged + edited body)
$ python3 digest.py --config config.toml --db digest.sqlite3
sent 2 items   # only the genuinely-new "Union responds to port fee inquiry" item
```
Exactly the newsroom `utm_campaign` rotation, wire `guid` regeneration, and blogroll edit were all correctly suppressed; only the truly new newsroom entry (`entry_id: 84130`) went out. This is the fallback-chain-quality solution the top band asks for, with the failure mode ("if a provider ever used query params as part of identity, we'd conflate two items") stated in the README (`README.md:57-59`). Not a full 10 because the identity is a single global rule (path-only) rather than a stated per-feed strategy — it happens to work for all three current feeds, but the design doesn't leave a documented seam for a fourth feed that *does* need query-string identity.

### Ambiguity handling — 5 / 8

- **First-run backfill**: named and handled explicitly. `README.md:61-68` ("First run after a deploy") and the `_migrate` function's docstring (`store.py:69-83`) both describe that an existing archive is treated as already-seen, so deploying this fix doesn't cause a resend storm. Top-band quality for this one fork.
- **Edited item as new vs. same**: decided silently as "same" (via `link_key`, which ignores everything but the path) — and it's the defensible choice — but it is never named as a decision with a tradeoff. The README documents *why* wire's `guid` can't be trusted, but never says outright "as a consequence, if wire republishes an item with corrected/updated body text, no one gets notified of the edit." That's exactly the kind of fork the rubric wants surfaced, and it isn't.
- **Per-channel vs. global suppression**: not named at all, anywhere. Look at `digest.py:36-46`:
```python
for feed_cfg in cfg["feeds"]:
    ...
    fresh = store.unseen_items(db, feed_cfg["name"], items)
    new_items.extend(fresh)
    if not dry_run:
        store.record_items(db, feed_cfg["name"], fresh)
```
Suppression is keyed on `(source, link_key)` only — global across all channels. That's a reasonable default and it isn't visibly broken for the current config (every item that matches any channel is delivered to all matching channels in the same run it first appears), but there's a real edge case the agent never mentions: add a new channel later, and it will never receive any item published before its addition, even though from that channel's point of view nothing has ever been sent. Nothing in the code or docs acknowledges this is a choice at all.

One fork fully named and handled, one silently-but-defensibly decided, one never recognized as a fork in the first place — solidly mid-band.

### Failure-mode reasoning — 1.5 / 8

This is the weak point. `digest.py:36-46` marks items as delivered (`store.record_items`) inside the *feed-fetch* loop, entirely before the channel-delivery loop (`digest.py:49-58`) even starts. `channels.py`'s own (untouched) docstring says:

```python
"""Delivery channels.
...
Delivery is best effort: if a channel is down we let the
exception propagate and cron will pick us up again on the next tick.
"""
```

That promise is now false. I reproduced it directly: set the `energy` channel to an unreachable webhook, run once (feeds all recorded as seen for this tick, `ops` channel gets its digest, then `energy`'s webhook throws `DeliveryError`, the process crashes before `everything` is ever attempted):

```
=== ops ===
Ops digest — 3 item(s)
...
channels.DeliveryError: energy: <urlopen error [Errno 61] Connection refused>
EXIT: 1
```

Fix the webhook (or, on the real cron job, wait for the next tick) and re-run — the `energy` and `everything` channels, which never got their digest, now get nothing, forever, because the items were already marked `link_key`-seen in the same run that crashed:

```
=== retry run ===
sent 0 items
```

The port-fee/tender items are silently and permanently lost to two of the three channels. This is not "per-run marking" in the tame sense the rubric describes (whole batch re-sent or dropped uniformly) — it's marking that happens *before delivery is even attempted*, so a single flaky channel drops other channels' copies of the same batch with no way to recover short of manually editing the database. There is no discussion anywhere in the diff of this ordering, of at-least-once vs at-most-once, or of what "channel two fails after channel one succeeds" should do. The recording-order comment that does exist (`digest.py:40-41`, "Recording happens after the selection so that --dry-run previews without consuming anything") only reasons about dry-run, not about delivery failure — so the ordering bug reads as an oversight, not a considered tradeoff.

### Existing-code respect — 5.5 / 6

`feeds.py`, `channels.py`, and `render.py` are byte-for-byte untouched (confirmed via diff). `store.py`'s `items` archive is reused rather than replaced, and the schema change is handled with a real migration (`store.py:69-98`) that adds `link_key`, backfills it from `link`, and collapses the duplicate rows the old archive-everything behavior had left behind — this is exactly the kind of care the rubric calls out ("if the unconditional insert is dealt with"). `--dry-run` was checked and still works with zero side effects (verified: `sent 0 items` and `SELECT COUNT(*) FROM items` = 0 after a dry run). Docked half a point for the ordering issue above, which sits in `digest.py`'s existing loop structure rather than being an addition, but does represent working *against* the grain of `channels.py`'s existing failure-handling contract.

### Code quality — 3.5 / 4

Clean, readable diff. `unseen_items` (`store.py:101-123`) correctly collapses in-batch duplicates as well as cross-run ones. SQL is sensible (`INSERT OR IGNORE`, a real unique index, parameterized `IN (...)` query). No dead code or vestigial columns — the old schema's columns are all still meaningfully used. Docked half a point because the migration silently discards duplicate `first_seen` timestamps' provenance (`DELETE FROM items WHERE id NOT IN (SELECT MIN(id) ...)`, `store.py:92-95`) with no comment on what happens to a mid-collapse crash (SQLite executescript/commit ordering means a crash between the `DELETE` and the index creation would leave the DB in a technically-recoverable but undocumented intermediate state).

### Documentation — 3 / 4

Well beyond a docstring: `README.md:31-68` is a dedicated "How dedup works" section with a per-feed identifier problem table, an explicit statement of the `link_key` trade-off, and a first-run/migration explanation. It does not, however, say anything about *resetting* the dedup memory (e.g., "delete the `items` table / drop `digest.sqlite3` to force a resend") which the rubric explicitly asks for, and it says nothing about the crash/retry behavior — which, given the bug above, is the one thing an on-call engineer most needs explained.

## 3. What it missed

- **The ordering bug is the big one** (see Failure-mode reasoning above): recording happens before delivery, not per-channel-after-delivery, silently changing the "best effort, cron retries" contract that `channels.py` already promised.
- **Per-channel vs. global suppression** was never named as a decision, let alone argued. The chosen behavior (global) is reasonable but has a real edge case (new channel added later gets no backfill) that isn't mentioned anywhere.
- **Edited item = same item** is the natural consequence of `link_key`, but the agent never states it as a decision with a cost ("if wire fixes a typo or adds a correction, no one is renotified").
- **No reset instructions** in the README, despite the rubric explicitly asking for this and it being a one-line addition (`rm digest.sqlite3` or a `DELETE FROM items`).
- **No discussion of at-least-once vs at-most-once** anywhere, and the two are in tension here: the current design is neither cleanly — it's "at-most-once, but the loss window is the entire multi-channel fanout of a single tick," which is the worst of both.

## 4. Bugs

**Data loss on partial channel failure.** Reproduction (from a clean checkout with `fixtures/snapshot-a`):

1. Point one channel at an unreachable webhook, e.g. add to `config.toml`:
   ```
   [[channels]]
   name = "energy"
   type = "webhook"
   url = "http://127.0.0.1:1/nope"
   keywords = ["tender", "grid", "offshore"]
   ```
2. Run `python3 digest.py --config config.toml --db digest.sqlite3`. Output: `ops` channel receives its digest, then the process crashes with `channels.DeliveryError: energy: <urlopen error [Errno 61] Connection refused>` (exit code 1), before the `everything` channel is ever reached.
3. Fix the channel (e.g. set it back to `type = "stdout"`) and re-run the exact same command. Output: `sent 0 items`.

The items destined for `energy` and `everything` (the offshore-tender / grid items and the full firehose) are gone permanently — they were already written into `items` via `store.record_items` during step 2, inside the feed-fetch loop, before any channel had been attempted. No amount of retrying will recover them; the operator would have to manually delete rows from the `items` table (an operation the README doesn't document) to get them re-sent. This is a direct regression against `channels.py`'s own documented guarantee that a down channel just waits for the next cron tick.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 5,
  "failure_mode_reasoning": 1.5,
  "existing_code_respect": 5.5,
  "code_quality": 3.5,
  "documentation": 3,
  "total": 27.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Great identity/migration work undone by marking-before-delivery data loss on channel failure."
}
```
