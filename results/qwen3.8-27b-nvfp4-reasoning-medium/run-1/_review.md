# Review — notify-digest dedupe fix

## 1. Summary

The agent added a `sent` table keyed on `(channel, source, link)` and an identity
rule (`raw_id` OR `link`, per source) used both to filter what gets archived
in `items` and to filter what gets (re-)sent per channel, writing the mark
only after a successful `channels.send`. It left `feeds.py`, `channels.py`,
`render.py` untouched, kept `--dry-run` non-mutating for the new table, and
documented the identity/scope/retry decisions in both `store.py`'s docstring
and a new README section. I would merge this: it is correct against both
fixture snapshots, doesn't bulldoze the existing code, and explains itself —
the main gaps are that the README never says how to reset the dedupe state or
what a brand-new deployment's first run looks like.

## 2. Per-category scoring

### Identity strategy — 9.5 / 10

The rule is `(source, raw_id)` OR `(source, link)`, implemented once and
shared between the archive and the per-channel sent-check:

```python
# store.py:72-80
def item_seen(known_ids, known_links, item):
    """True if `item` matches something already recorded.

    Matches on raw_id OR link; see the module docstring for why both.
    """
    raw_id = item.get("raw_id")
    if raw_id and raw_id in known_ids:
        return True
    return item["link"] in known_links
```

I ran it against both fixture snapshots (`config.example.toml` → snapshot-a,
then a copy pointed at snapshot-b) with a persistent `digest.sqlite3`:

```
$ python3 digest.py --config config.example.toml --db digest.sqlite3
sent 12 items
$ python3 digest.py --config config-b.toml --db digest.sqlite3
sent 2 items          # only the genuinely-new "Union responds..." item
$ python3 digest.py --config config-b.toml --db digest.sqlite3   # third tick, same data
sent 0 items
```

Snapshot-b rotates the newsroom item's `utm_campaign` (caught by `raw_id`
match), regenerates the wire item's `guid` (caught by `link` match), and
edits the blogroll title with no id at all (caught by `link` match) — all
three are suppressed correctly, and the actually-new newsroom item goes
through. The failure mode is stated explicitly, not just implied:

```
# README.md:48-50
either. If a feed ever changes an item's link *and* its id, that item
will be sent once more — accepted trade-off, since link+id is the most
stable combination the providers offer.
```

Docking half a point only because "stable combination" is asserted rather
than proven for future feed behavior — there's no guard against a fourth
feed with neither field, though none exists in the fixtures.

### Ambiguity handling — 6 / 8

Two of the three forks are named explicitly, one is decided silently.

- **Per-channel vs. global suppression** — named and justified:
  ```
  # store.py:26-29
  ``sent`` is keyed per channel on purpose: if a channel's keyword list
  changes or a new channel is added, its next run should still get the
  items that match it; and a failed delivery must not count as sent, so
  the next tick retries it.
  ```
- **Edited item as new vs. same** — named, with the per-feed table in the
  README (lines 40-44) explaining exactly which field is stable per feed.
- **First-run backfill** — never discussed. On a brand-new `digest.sqlite3`,
  `sent` is empty, so the very first invocation sends *everything currently
  in the feed* to every matching channel as if it were all new. That's a
  defensible default (verified above: 12 items went out on the very first
  run against snapshot-a) but nobody says so, and nobody warns that pointing
  the tool at a feed with a long backlog will dump the whole backlog on
  first tick.

### Failure-mode reasoning — 7.5 / 8

Marking happens after delivery, per channel, and the ordering is argued in
comments:

```python
# digest.py:65-70
channels.send(chan_cfg, body)
# Only after a successful send: if delivery raises, the next tick
# retries these items (and other channels are unaffected, since
# `sent` is per channel).
store.mark_sent(db, chan_cfg["name"], selected)
sent += len(selected)
```

I forced a delivery failure on the first channel (webhook to a closed port)
with two feeds' worth of new items in flight:

```
raised: DeliveryError ops: <urlopen error [Errno 61] Connection refused>
sent rows: []
```

Nothing was marked sent, the `items` archive commit for that source was
already durable (`record_items` commits per feed, before the channel loop
even starts), and a second run with a working webhook would resend exactly
the missed batch — at-least-once, correctly. The one imprecision: the code
comment says "other channels are unaffected" — true for their *sent-state*,
but since the exception propagates out of `run_once` and crashes the process
(matching `channels.py`'s pre-existing "let it propagate, cron will retry"
design), channels *after* the failing one in the same tick don't get sent
until the next 15-minute tick either, they just aren't corrupted. The
guarantee is right; the comment is slightly optimistic about what "unaffected"
means. Half a point off for that plus the lack of any explicit at-least-once
vs. at-most-once framing (it's implied, not named).

### Existing-code respect — 6 / 6

`feeds.py`, `channels.py`, and `render.py` are byte-identical to the
original. `store.py`'s schema change is additive (`CREATE TABLE IF NOT
EXISTS sent ...` next to the untouched `items` table), and the rubric's
named concern — "reusing the `items` archive is fine if the unconditional
insert is dealt with" — is handled directly:

```python
# store.py:99-104
def record_items(conn, source, items):
    """Archive items we have not seen before (see `item_seen`)."""
    known_ids, known_links = archived_pairs(conn, source)
    new = [i for i in items if not item_seen(known_ids, known_links, i)]
    if not new:
        return
```

`--dry-run` still works and was verified not to write to `sent`:

```
$ python3 digest.py --config config.example.toml --db digest.sqlite3 --dry-run
$ sqlite3 ...  sent rows after dry-run: 0
```

No scope creep — no plugin system, no scheduler, nothing beyond what the
prompt asked for.

### Code quality — 3.5 / 4

Clean, small diff, decent naming (`archived_pairs` / `sent_pairs` mirror each
other), no dead code. `mark_sent` duplicates `title` into the `sent` table
purely for debuggability:

```python
# store.py:122-129
conn.executemany(
    "INSERT OR IGNORE INTO sent (channel, source, raw_id, link, title)"
    " VALUES (?, ?, ?, ?, ?)",
    ...
```

That's a minor, harmless redundancy against the rubric's "no vestigial
columns" bar rather than a real defect — half a point off for it. Schema
migration is handled correctly (`CREATE TABLE IF NOT EXISTS`, no destructive
change to `items`).

### Documentation — 3 / 4

More than a docstring: the README gets a full "Deduplication" section
(README.md:31-64) that explains the identity rule, the per-channel table
rationale, and the dry-run/mark interaction. It does **not**, however,
explain how to reset the dedupe state (e.g., resend everything to a channel
by clearing its `sent` rows, or start fresh by deleting the db) or spell out
first-run behavior — both explicitly called out by the rubric. Solid but
incomplete; capped below full marks for the two missing pieces, not down to
docstring-level because the explanation that exists is genuinely good.

## 3. What it missed

- **First-run backfill** is never named as a decision (see Ambiguity
  handling above) — it happens to behave sensibly (send whatever the feed
  currently has), but a fresh deployment against a feed with a long backlog
  will flood every channel on the first tick with no warning in the docs.
- **No reset instructions.** If someone needs to force-resend to one channel
  (e.g., after a bad send that somehow got marked, or to test a channel
  change), the README doesn't say "delete rows from `sent` where
  channel = ...". That's a one-line addition the rubric explicitly asks for.
- **Within-tick channel ordering on failure** isn't discussed: if channel 1
  raises, channels 2 and 3 don't get attempted *this* tick even though they
  have new items ready — they're simply picked up on the next tick. This
  matches the pre-existing `channels.py` design intent ("let the exception
  propagate") so it isn't a regression, but the new code comment ("other
  channels are unaffected") slightly overstates what's actually guaranteed.
- **Feed self-duplication** isn't handled: if a single feed fetch itself
  contained two entries with the same identity (not exercised by the
  fixtures), both would pass `item_seen` in the same run since `known_ids` /
  `known_links` are computed once before the batch, not updated as the batch
  is processed. Harmless with these fixtures, unaddressed as a case.

## 4. Bugs

None found. Both fixture snapshots run cleanly through two consecutive
"ticks" (`snapshot-a` then `snapshot-b` against the same db) and produce
exactly the expected diff (one genuinely new newsroom item, all edits to
existing items suppressed); a third run against unchanged data sends 0
items; `--dry-run` doesn't mutate `sent`; a forced delivery failure leaves
no partial `sent` rows and is retryable. `python3 -m py_compile` passes on
all files.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 7.5,
  "existing_code_respect": 6,
  "code_quality": 3.5,
  "documentation": 3,
  "total": 35.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Correct, well-tested dedupe with sound identity rule; README skips reset and first-run notes."
}
```
