# Review — notify-digest, flint-nvfp4 run-4

## 1. Summary

The agent correctly diagnosed the root cause — `digest.py` recorded every
fetched item unconditionally and never consulted the archive before sending,
so the same feed content went out on every single cron tick — and fixed it
with a minimal, surgical diff: `store.filter_new()` gates what reaches the
channel loop, and an item is only written back to the archive after a channel
successfully sends it. The identity key (`source`, `raw_id or link`) correctly
survives the `newsroom` feed's rotating `utm_campaign` and the `blogroll`
feed's missing `raw_id`, but it does not survive the `wire` feed's
regenerated `guid` on edit — the one failure mode `feeds.py` explicitly warns
about in a comment two lines above where the agent wrote the matching code —
and it silently introduces a new bug where a channel that fails after a
sibling channel has already succeeded for the same item never gets a retry,
ever. I would merge this with fixes required: the core mechanism is sound and
the diff is respectful, but the wire-feed duplicate and the cross-channel
silent-drop are the exact class of defect the task was filed to prevent.

## 2. Per-category scoring

### Identity strategy — 6 / 10

`store.py:35-41`:
```python
def item_key(source, raw_id, link):
    """Identity of an item: its source plus a stable per-item identifier.

    Feeds that have no stable identifier fall back to the item's canonical
    link.
    """
    return (source, raw_id or link)
```

I ran this against both fixtures with the agent's actual code (scratch copy
in `/tmp/notify-test`, config pointed at `snapshot-a` then `snapshot-b`
against the same db):

- `newsroom`: `entry_id` is stable across snapshots even though the URL's
  `utm_campaign` rotates (`w33` → `w34`). Correctly suppressed both carried-
  over items in the second run, correctly surfaced the one truly new item
  (`84130`). This is the strategy working as intended.
- `blogroll`: has no `raw_id` at all, falls back to `link`. The post's title
  and excerpt change between snapshots ("Notes on port fee arithmetic" →
  "... (updated)") but the permalink doesn't, so it's correctly treated as
  the same item and not resent.
- `wire`: **breaks**. `feeds.py:71-72`, untouched by the agent, says outright:
  `# "generic": has a guid, but the provider regenerates it whenever an item
  is edited (typos, added tags, retitles).` Snapshot-a's "Port fee inquiry
  opened" has `guid: wire-2026-08-14-0031`; snapshot-b's edited version of
  the same link has `guid: wire-2026-08-14-0031-r2`. Since `raw_id` is
  present, `item_key` prefers it over the link, so the edited item gets a
  brand new identity and is delivered a second time. Reproduction below.

This is a single strategy that handles two of three feeds and breaks,
unnoticed and undiscussed, on the third — which happens to be the one feed
in the whole codebase that has a code comment warning about exactly this.
Per the rubric this sits in the 6-8 band; I put it at the bottom of the band
because the failure mode was already flagged in the file the agent had to
read to write `feeds.fetch`, and the README's description of the identity
scheme ("a stable per-item id") asserts stability it doesn't actually have
for this feed.

### Ambiguity handling — 2.5 / 8

None of the three forks are named in the README or in code comments. Of the
decisions made silently:

- **Per-channel vs global suppression** — decided silently, and **wrong**.
  `digest.py:57-62` marks an item as sent as soon as *any* channel delivers
  it, with no per-channel record:
  ```python
  for item in selected:
      key = store.item_key(item["source"], item.get("raw_id"), item.get("link"))
      if key in marked:
          continue
      marked.add(key)
      store.record_items(db, item["source"], [item])
  ```
  Since `config.example.toml` has an `everything` channel with empty
  keywords, nearly every item matches at least two channels. If an earlier
  channel succeeds and a later channel for the same item fails, the item is
  already in the archive and will never be offered to the failing channel
  again. Verified by reproduction below — this is the scoring-notes' own
  example of "typically global suppression."
- **Edited item as new vs same** — not a deliberate choice, just a side
  effect of the identity scheme. It happens to come out "same" for
  `blogroll` (good) and "new" for `wire` (bug, see above). Inconsistent, not
  argued anywhere.
- **First-run backfill** — not addressed at all. On a fresh `digest.sqlite3`,
  whatever the feeds currently contain is treated as 100% new and fired at
  every matching channel in one run. For the two-item-per-feed fixtures this
  is invisible; for a real feed with months of backlog on first deploy it
  would flood every channel. The README does not mention this.

This matches the rubric's 2-3 band precisely: decided silently, and at least
one decision (global suppression) is wrong.

### Failure-mode reasoning — 3 / 8

`digest.py:46-62` (same block as above). There is no per-channel marking —
marking is per-*item*, independent of which channel(s) actually received it.
`channels.send()` is still called with no surrounding `try/except`
(unchanged from the original), so a failing channel's exception propagates
out of `run_once` and `main`, aborting the run and skipping every channel
later in `cfg["channels"]` for that tick. Combined with the global-marking
bug above, this produces a genuine partial-failure data loss window, not
just a delay:

Reproduction (`/tmp/notify-test`, `config-fail.toml`): two channels, `ops`
(stdout, keyword `"port"`) then `firehose` (webhook, deliberately
unreachable). Feed is `wire` snapshot-a (2 items, only item 1 matches
`"port"`, both match `firehose`'s empty filter).

```
$ python3 digest.py --config config-fail.toml --db fail.sqlite3
=== ops ===
Ops digest — 1 item(s)
* Port fee inquiry opened  [wire]  ...
...
channels.DeliveryError: firehose: <urlopen error [Errno 61] Connection refused>
```
`ops` succeeded and item 1 ("Port fee inquiry opened") is now in the
archive — marked by `ops`'s success, never actually delivered by `firehose`.
`sqlite3 fail.sqlite3 "select source, raw_id, link from items"` after this
run shows only that one row. Point `firehose` at a webhook that now works
and re-run with the *same* db: item 1 is never retried for `firehose` — it's
gone forever for that channel, even though `firehose` never got it. (Item 2,
which never matched `ops`, correctly does retry and succeeds once
`firehose` recovers.) This is "a partial-failure window that loses ... a
whole batch" for the overlapping subset of items — rubric 2-3 band, and the
rubric's own scoring notes name this exact defect ("if it also drops items
on retry, a failure-mode one (3)").

### Existing-code respect — 5.5 / 6

The diff is minimal and targeted: `feeds.py`, `channels.py`, `render.py`,
and `config.example.toml` are byte-for-byte untouched (verified with
`diff`). The unconditional insert the rubric calls out by name is dealt
with — `digest.py:40` now calls `store.filter_new()` instead of
`store.record_items()` at fetch time, and `record_items` is deferred until
after a successful `channels.send()`. `--dry-run` was checked by hand: a
dry run against an empty db writes nothing (`SELECT count(*) FROM items` →
`0`), and a subsequent real run still sends and marks all 6 fixture items —
dry-run doesn't poison the dedup state. No new table, no schema bump. Docked
half a point only because the fix that was added (global, per-item marking)
is itself the source of the failure-mode bug above — the respect for the
codebase's shape is real, but the change it layered on top wasn't fully
thought through.

### Code quality — 3.5 / 4

New functions (`item_key`, `seen_keys`, `filter_new`) are short, named well,
and documented with one-line docstrings that state their contract and
fallback behavior. No vestigial columns, no schema migration needed since
the table shape didn't change. Minor deduction: `seen_keys` does a full
`SELECT source, raw_id, link FROM items` (optionally filtered by `source`)
on every feed, every tick — correct, but with no index to back the lookup
other than the pre-existing `idx_items_source`; fine at fixture scale,
something to flag before the archive grows past a few years of 15-minute
polling. `count_items` (pre-existing, already dead in the original) remains
unused and wasn't cleaned up, but that's inherited, not introduced.

### Documentation — 2.5 / 4

`README.md:17-28` adds a dedicated section, not just a docstring, so it
clears the 2-point docstring cap. It explains the mechanism (filter before
channel loop) and the retry contract ("if sending raises, the error
propagates and the item is not marked as sent, so the next tick retries
it") reasonably clearly, though that sentence is true only at the *whole-run*
level given the global-marking bug above, not per-channel as the prose
implies. It does **not** say how to reset the dedup state (e.g., delete or
truncate `digest.sqlite3`) and does **not** discuss first-run behavior
(whatever's currently in the feed is treated as new the first time around).
Two of the three things the rubric asks the README to cover are simply
absent.

**Total: 6 + 2.5 + 3 + 5.5 + 3.5 + 2.5 = 23 / 40**

## 3. What it missed

- No mention anywhere (code or docs) that `guid` on the `wire`/`generic` feed
  is not a stable identifier across edits — despite `feeds.py` saying so in
  a comment the agent had to pass to write the matching `raw_id` extraction.
- No per-channel send-state tracking, so the per-channel-vs-global fork was
  never surfaced as a decision at all — it fell out of how the loop happened
  to be structured.
- First-run backfill: no discussion, no flag to suppress or cap it, no
  README note warning an operator that pointing this at a long-lived feed
  for the first time will fan out the whole backlog to every channel at
  once.
- No "how to reset" story in the README — if duplicates do slip through
  (they do, for `wire`) or someone wants to resend a channel's history, the
  only option is to go delete rows out of `digest.sqlite3` by hand, and the
  README doesn't say so.
- No index or uniqueness constraint added on `(source, raw_id)` / `(source,
  link)` even though those pairs are now load-bearing for correctness, not
  just an archive nicety.

## 4. Bugs

**Bug 1 — duplicate delivery survives the fix, for the exact feed whose
comment warns about it.** `wire`'s `guid` rotates when an item is edited.
Running snapshot-a then snapshot-b through the agent's code with the same
db:

```
$ python3 digest.py --config config-a.toml --db digest.sqlite3   # first tick
...
* Port fee inquiry opened  [wire]
  Regulator confirms inquiry.
  https://wire.example/i/0031
...
sent 6 items

$ python3 digest.py --config config-b.toml --db digest.sqlite3   # next tick
=== everything ===
Firehose — 2 item(s)
* Union responds to port fee inquiry  [newsroom]  ...
* Port fee inquiry opened  [wire]
  Regulator confirms inquiry. Adds operator comment.
  https://wire.example/i/0031
sent 2 items
```
"Port fee inquiry opened" is the same real-world story at the same link,
delivered twice 15 minutes apart — precisely the complaint in the task
prompt, un-fixed for this feed.

**Bug 2 — an item can be permanently dropped for a channel that never
received it, if a sibling channel for the same item succeeds first.**
Two-channel config, `ops` (succeeds) before `firehose` (fails this tick):
```
$ python3 digest.py --config config-fail.toml --db fail.sqlite3
=== ops ===
Ops digest — 1 item(s)
* Port fee inquiry opened  [wire] ...
Traceback ...
channels.DeliveryError: firehose: <urlopen error [Errno 61] Connection refused>
$ sqlite3 fail.sqlite3 "select source, raw_id, link from items;"
wire|wire-2026-08-14-0031|https://wire.example/i/0031
```
Bring `firehose`'s endpoint back up and re-run against the same db: the item
is gone from `all_items` (already in the archive via `ops`) and is never
offered to `firehose` again — confirmed with a second fixture item that
*wasn't* claimed by `ops` first, which does correctly retry and succeed once
`firehose` recovers. `firehose` permanently never gets the item it was
supposed to receive, with no error, warning, or log line indicating a drop
occurred.

```json
{
  "identity_strategy": 6,
  "ambiguity_handling": 2.5,
  "failure_mode_reasoning": 3,
  "existing_code_respect": 5.5,
  "code_quality": 3.5,
  "documentation": 2.5,
  "total": 23,
  "would_merge": "yes-with-fixes",
  "one_line": "Fixes the resend loop but misses guid rotation and silently drops cross-channel retries."
}
```
