# Review: notify-digest dedupe fix (flint-nvfp4, run-3)

## 1. Summary

The agent gave every feed a real identity (`entry_id` for newsroom, `permalink`
for blogroll, canonical `link` for the guid-churning generic/wire feed),
enforced it with a `UNIQUE` index plus a migration that collapses
pre-existing duplicate rows, and moved the "mark as sent" write to *after*
each channel's delivery succeeds. That core fix is correct and verified
against both fixture snapshots, including the edited-item cases. It is let
down by a real, reproducible bug: because "seen" is tracked per
`(source, raw_id)` and not per `(channel, source, raw_id)`, an item that is
marked seen by channel 1 is permanently invisible to channel 3 if channel 2
threw an exception in between and aborted the run before channel 3 ran — a
silent, permanent loss for a channel that never actually received the item.
I would merge this as **yes-with-fixes**: the identity work is good enough to
build on, but the cross-channel suppression bug needs a fix (query
`unseen_items` per channel, or key the archive by channel) before it goes near
a multi-channel config in production.

## 2. Per-category scoring

### Identity strategy — 9 / 10

Each of the three feed formats gets its own reasoned identity in
`feeds.py`, and all three survive the scenarios the fixtures are built to
test:

```python
# feeds.py:57-69  (blogroll)
if fmt == "blogroll":
    # The permalink is the only stable identifier this feed offers.
    return [
        {
            ...
            "raw_id": r["permalink"],
        }
        for r in doc.get("posts", [])
    ]

# feeds.py:71-84  (generic/wire)
# "generic": has a guid, but the provider regenerates it whenever an
# item is edited (typos, added tags, retitles), so the canonical link is
# the only usable identity.
return [
    {
        ...
        "raw_id": r.get("link"),
    }
    for r in doc.get("items", [])
]
```

`store.py` backs this with a fallback chain and a unique index:

```python
# store.py:34-35
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_raw
    ON items(source, COALESCE(raw_id, link));
```

I verified this against the actual fixtures (`/tmp` scratch copy, Python
3.13):

- **Rotating `utm_campaign`**: `newsroom.json` snapshot-a → snapshot-b changes
  `utm_campaign=w33` to `w34` on every URL. Identity for `newsroom` is
  `entry_id`, not the URL, so this is a non-event — confirmed: items 84121 and
  84118 were not re-sent on the snapshot-b run.
- **Missing `raw_id`**: the original `blogroll` entries had `raw_id: None`.
  The agent fixed this at the source by setting `raw_id = permalink`, and
  `store.py`'s `COALESCE(raw_id, link)` is still there as a second line of
  defence for anything that slips through with a null id.
- **Regenerated `guid`**: `wire.json`'s guid goes from
  `wire-2026-08-14-0031` to `wire-2026-08-14-0031-r2` between snapshots (the
  item was edited — summary gained "Adds operator comment"). Since identity
  is the `link`, not the `guid`, the edited item was correctly treated as
  already-seen and was *not* re-sent.

Ran it end to end: first run against `snapshot-a` sent 12 items; a second run
against the same `snapshot-a` sent 0; a third run switched to `snapshot-b`
and sent exactly the 1 genuinely new item (`entry_id` 84130, "Union responds
to port fee inquiry") — the edited blogroll post and edited wire item were
both correctly suppressed.

Docked one point because the "stated with failure modes" bar isn't fully met:
the comments explain *why* each identity was picked, but never flag the
residual risk — e.g. that the generic feed's dedupe now rests entirely on
`link` staying byte-for-byte stable, so if that provider ever starts
appending its own tracking params to `link` (the way newsroom does to `url`),
dedupe silently breaks again. That's a one-sentence addition that's missing.

### Ambiguity handling — 2.5 / 8

Of the three named forks in the rubric, only one gets any explicit treatment,
and it's partial; the other two are undiscussed, and one of them is
demonstrably wrong (see Failure-mode reasoning and Bugs below).

- **Edited item as new vs same**: named and reasoned about for the
  generic/wire feed (`feeds.py:71-73`, quoted above). Not extended to the
  blogroll feed, which has the exact same property in the fixtures
  (`snapshot-b/blogroll.json`'s "Notes on port fee arithmetic (updated)" has
  the same permalink as snapshot-a's version) — the comment there
  (`feeds.py:58`) just says "the permalink is the only stable identifier,"
  with no mention that this also means edits won't be re-flagged. Not
  mentioned in the README at all.
- **Per-channel vs global suppression**: never named anywhere. The
  implementation is global — `unseen_items(db, feed_cfg["name"], items)` in
  `digest.py:44` filters by `source` only, with no channel dimension, and
  `record_items` writes to the same `(source, raw_id)`-keyed archive
  regardless of which channel's loop iteration triggered it
  (`digest.py:61-64`). As shown below, this is not just "silent," it is the
  thing that causes a channel to permanently lose items it never received.
- **First-run backfill**: never named anywhere. Confirmed by running against
  an empty database: the very first run sends *everything* currently in all
  three feeds (12 items in the fixtures) as a normal digest, with no flag to
  seed the archive without sending and no comment warning that a first
  deploy against a feed with real history will blast out its entire backlog.

This lands at the bottom of the "decided silently" band rather than the
middle, because the scoring notes are explicit that a global-suppression
default of this kind is "typically wrong," and here it is — not just in
theory but in a one-line reproduction (see Bugs).

### Failure-mode reasoning — 3 / 8

The agent gets credit for per-channel marking with the right ordering inside
a single channel's own path — send, then mark, with the reasoning written
down twice:

```python
# digest.py:59-64
channels.send(chan_cfg, body)
sent += len(selected)
# Mark what went out as seen so the next tick does not send it again.
# Items whose delivery failed are left unmarked and retried later.
for item in selected:
    store.record_items(db, item["source"], [item])
```

```markdown
<!-- README.md:39-41 -->
An item's identity is its `raw_id` (entry id, permalink, or canonical link,
depending on feed format). A run marks an item as seen only after its delivery
succeeded, so a failed channel does not cause the item to be silently dropped.
```

That claim — "a failed channel does not cause the item to be silently
dropped" — is true for the channel that actually failed. It is false for any
*other* channel that was supposed to run afterward in the same config. The
marking is keyed by `(source, raw_id)`, not by `(channel, source, raw_id)`,
and `channels.py`'s pre-existing (unchanged) contract is to let delivery
exceptions propagate and abort the whole run:

```python
# channels.py:1-6
"""Delivery channels.
...
Delivery is best effort: if a channel is down we let the exception propagate
and cron will pick us up again on the next tick.
"""
```

Combine an uncaught exception that aborts the `for chan_cfg in cfg["channels"]`
loop (`digest.py:50-64`) with an earlier channel in that same loop having
already sent-and-marked an item, and a later channel that never got to run
this tick will *never* get that item — not on the retry, not ever — because
the item has already left the "unseen" pool by the time the retry's
`unseen_items` query runs. This is a genuine at-least-once violation masquerading
as one, with the actual behavior landing on data loss for a subset of
channels. See Bugs for the full reproduction.

### Existing-code respect — 5 / 6

The agent worked inside the grain of the codebase rather than against it:
`feeds.py`'s three-branch structure is untouched except for three `raw_id`
values and their comments; `channels.py` and `render.py` are byte-identical;
`config.example.toml` is untouched (no scope creep — no new config knobs, no
plugin system). `--dry-run` still works and, cross-checking against the
original, actually becomes *more* honest: the original called
`store.record_items` unconditionally before checking `dry_run`, so a "dry"
run was quietly writing to the archive; the new code only calls
`unseen_items` (a pure read) when `dry_run` is set (`digest.py:44`), so dry
runs are now fully non-mutating. That's an improvement, but it's an
undocumented behavior change to a flag whose contract ("prints what would be
sent instead of sending it") arguably implied non-mutation all along, so I'm
not docking for it — just noting it for the next reader.

Docked half a point for two things: the schema migration
(`store.py:28-32`, the `DELETE ... GROUP BY` dedupe pass) runs on every
single `connect()` call rather than being gated to run once, which is wasteful
on every 15-minute tick even once there's nothing left to collapse; and the
per-item `for item in selected: store.record_items(db, item["source"], [item])`
loop (`digest.py:63-64`) calls a batch-oriented function with one-item
batches, doing N individual `INSERT OR IGNORE` + N individual commits where
one batched call would do.

### Code quality — 3.5 / 4

Readable, no new dead code, no scope creep. The migration itself is good
craftsmanship — handled for a database that already has the "quiet bug"'s
accumulated duplicates in it, not just for a fresh one:

```python
# store.py:28-35
-- Collapse duplicates from before uniqueness was enforced, so the unique
-- index below can be created on databases that already have repeats in them.
DELETE FROM items WHERE id NOT IN (
    SELECT MIN(id) FROM items GROUP BY source, COALESCE(raw_id, link)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_raw
    ON items(source, COALESCE(raw_id, link));
```

I built a throwaway "legacy" DB with five duplicate rows for the same item
(simulating five old cron ticks before this fix existed) and ran `store.connect`
against it: it collapsed to one row, confirming the migration does what it
says. Minor smell: `raw_id` is now identical to `link` for two of the three
feed formats (blogroll, generic), so the column carries no information for
those feeds beyond what `link` already has — not wrong, just a little
redundant going forward.

### Documentation — 2 / 4

The README gets a real section, not just a docstring, and it correctly
explains the headline mechanism:

```markdown
<!-- README.md:31-41 -->
## How new items are decided

Each run fetches the configured feeds and asks the SQLite archive
(`digest.sqlite3`) which items it has not seen yet. Only unseen items are
selected, rendered, and sent; anything already in the archive is skipped. ...
```

But of the three things the rubric asks for — dedupe behavior, how to reset
it, what happens on first run — only the first is covered. There is no
mention anywhere of deleting/rotating `digest.sqlite3` to replay history, and
no mention that the very first run against a populated feed will send its
entire current backlog as one digest. Both are one-paragraph additions that
are simply absent.

## 3. What it missed

- **Per-channel suppression state.** The single biggest silent decision: an
  item is "seen" the moment any channel sends it, not when a specific channel
  sends it. Fine in the common case (all channels process the same in-memory
  item list in one pass), broken the moment a channel in between throws.
- **First-run behavior.** No backfill flag, no warning, no decision recorded
  anywhere that the first run against a live feed will send its whole visible
  history as a single digest.
- **Reset/replay instructions.** Nothing in the README about what to do if
  you want to resend history (delete the db file? there's no `--reset` flag,
  and no note on what state lives where).
- **Vestigial redundancy.** `raw_id` duplicates `link` for 2 of 3 feed
  formats post-fix; not cleaned up or commented on.
- **Migration running every tick** rather than once, with no version guard.

## 4. Bugs

**Cross-channel data loss on partial failure.** Reproduced with a 3-channel
config (`ops` → `broken` (bad webhook) → `everything`), one feed
(`newsroom`, snapshot-a):

```toml
[[channels]]
name = "ops"
keywords = ["port"]
[[channels]]
name = "broken"
type = "webhook"
url = "http://127.0.0.1:1/nope"   # always refuses
keywords = ["grid"]
[[channels]]
name = "everything"
keywords = []                     # catches everything, including "port"
```

Run 1: `ops` sends and marks the "port fees" item as seen. `broken` throws
`DeliveryError`, which is uncaught and propagates out of `run_once` —
confirmed exit code 1, unhandled traceback. `everything` never runs this
tick, so it never actually received the "port fees" item.

Run 2 (fix the webhook URL, run again): `broken` and `everything` both run
successfully this time — but `everything`'s digest contains only the item
that matches `broken`'s keywords ("grid"); the "port fees" item, which
`everything` was always supposed to receive and never did, is gone for good.
It is not in the archive as "sent to everything" — it's just gone, because
the archive doesn't distinguish channels.

```
$ python3 digest.py --config config-fail.toml --db digest-fail.sqlite3
=== ops ===
Ops digest — 1 item(s)
* Regulator opens inquiry into port fees ...
Traceback (most recent call last):
  ...
channels.DeliveryError: broken: <urlopen error [Errno 61] Connection refused>
exit code: 1

$ # fix the webhook, run again
$ python3 digest.py --config config-fail-fixed.toml --db digest-fail.sqlite3
=== broken ===
Broken — 1 item(s)
* Grid operator delays offshore tender ...
=== everything ===
Firehose — 1 item(s)
* Grid operator delays offshore tender ...
sent 2 items
```

`everything` should have 2 items (the grid item *and* the port item it
never got); it has 1. The "port fees" item was delivered exactly once, to
`ops`, and `everything` will never see it, despite `everything`'s whole
purpose being to catch every item that goes out. This is a direct
consequence of keying the archive by `(source, raw_id)` instead of
`(channel, source, raw_id)`.

This is not a contrived edge case for a cron job that runs every 15 minutes
against three independently-operated webhook endpoints — one flaky channel
is exactly the scenario the task description is implicitly worried about.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 2.5,
  "failure_mode_reasoning": 3,
  "existing_code_respect": 5,
  "code_quality": 3.5,
  "documentation": 2,
  "total": 25,
  "would_merge": "yes-with-fixes",
  "one_line": "Fixes repeat sends well, but global marking can silently drop items for other channels."
}
```
