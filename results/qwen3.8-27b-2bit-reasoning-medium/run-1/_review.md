# Review — notify-digest, run-1

## 1. Summary

The agent added a per-channel `sent` table (`source, link, channel`), keyed
items by `(source, link-with-query-stripped)` instead of the unreliable
per-feed `raw_id`, and marks items sent only after a successful delivery so a
down channel gets retried on the next tick rather than losing or duplicating
items. It touched only `digest.py`, `store.py`, and `README.md`, leaving
`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` byte-for-byte
identical, and it verifiably stops re-sending items across the two fixture
snapshots while still delivering genuinely new ones. I would merge this,
possibly with a follow-up note about first-run backfill and a reset
procedure, both of which are the only material gaps.

## 2. Per-category scoring

### Identity strategy — 9 / 10

`store.py:43-54`:

```python
def item_key(item):
    """The (source, link) pair that identifies an item across runs.

    Deliberately NOT raw_id: blogroll has no identifier at all, and the wire
    feed regenerates its guid whenever an item is edited (typos, retitles),
    which would re-send edited items. Deliberately NOT the raw link either:
    newsroom appends rotating utm_* tracking params to the same article, so
    the raw link changes week to week. Stripping the query string and
    fragment leaves a permalink that is stable for all three providers.
    """
    parts = urlsplit(item["link"])
    return item["source"], urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
```

I ran this against the actual fixtures (copied to `/tmp/nd_test`, not
modified in place):

- `snapshot-a` → `snapshot-a` again: `sent 12 items` then `sent 0 items` —
  full suppression, as expected.
- `snapshot-a` → `snapshot-b`: only the genuinely new `newsroom` item
  (`entry_id 84130`) went out, despite `snapshot-b` rotating every
  `utm_campaign=w33` → `w34` param and regenerating the `wire` guid
  (`wire-2026-08-14-0031` → `-r2`) for an edited item with the same link.

This is exactly the three failure modes the rubric asks about:
`raw_id` missing (blogroll), `guid` regeneration (wire), and query-string
churn (newsroom). The rationale is stated inline and in the README table
(`README.md:43-47`). I'm not giving full marks because it's a single
strategy, not a stated fallback chain — if a future feed's stable identifier
requires more than link-stripping (e.g. a feed that reuses the same path for
different articles), there's no documented escape hatch, and the docstring
doesn't discuss what breaks the chosen strategy itself (e.g. two truly
different articles that coincidentally share a path after stripping query
params). Minor, but the top band asks for the failure modes of the *chosen*
strategy, not just the alternatives it rejected.

### Ambiguity handling — 6 / 8

Three forks exist in this domain; the agent handles two explicitly and
leaves one silent.

1. **Per-channel vs global suppression** — resolved explicitly and correctly.
   `digest.py:49-56`:
   ```python
   for chan_cfg in cfg["channels"]:
       # Per-channel dedup: an item may match several channels and should
       # reach each of them exactly once, but never twice.
       seen = store.sent_keys(db, chan_cfg["name"])
       selected = [
           i for i in all_items
           if matches(i, chan_cfg) and store.item_key(i) not in seen
       ]
   ```
   Confirmed by the DB dump after my snapshot-a run — the same item
   (`newsroom/.../port-fees`) appears in `sent` for both `ops` and
   `everything`, independently.

2. **Edited item as new vs same** — resolved explicitly (edits are treated as
   the same item, not new), and stated as a deliberate choice both in the
   `item_key` docstring and in README.md:49-50 ("does not change when an item
   is edited in place"). Verified: the `wire` guid-regen item and the
   `blogroll` excerpt-updated item were both suppressed in the snapshot-b
   run.

3. **First-run backfill** — **not mentioned anywhere**. On a fresh DB, the
   `sent` table is empty, so the very first cron tick dumps every item
   currently in every feed to every matching channel at once. I reproduced
   this above (`sent 12 items` on the first run). Whether that's desired
   (probably yes, for a small feed set) is a real decision that was made
   silently — never named in code or README.

Two of three forks named and resolved correctly, one silent-but-plausible →
mid-band per rubric ("One or two named; the rest decided silently but
correctly").

### Failure-mode reasoning — 7 / 8

`digest.py:63-68`:

```python
channels.send(chan_cfg, body)
# Mark only after a successful send, so a failed channel is retried
# on the next tick instead of silently dropping the items.
store.mark_sent(db, chan_cfg["name"], selected)
sent += len(selected)
```

and `store.py:85-90`:

```python
def mark_sent(conn, channel, items):
    """Record that `items` were delivered to `channel`.

    Call this only after a successful send: if delivery fails, the items
    stay unmarked and get retried on the next cron tick.
    """
```

Marking happens per channel, after send, so a channel-2 failure after
channel-1 success leaves channel-1's items marked (not re-sent) and
channel-2's items unmarked (retried next tick) — correct at-least-once
semantics, and the tradeoff (retry-until-delivered vs. possible duplicate on
a race) is named in both the code comment and README.md:52-53. I stop short
of full marks because the discussion doesn't acknowledge that
`channels.send` raising `DeliveryError` (which `channels.py` explicitly does
by design) aborts `run_once` entirely — channels later in the loop than the
failing one are not attempted *this* tick at all, only on the next one. That
is a reasonable emergent behavior given the 15-minute cadence, but it's a
consequence of the pre-existing `channels.py` contract that the agent
inherited rather than reasoned through explicitly.

### Existing-code respect — 6 / 6

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are
byte-identical to the original (verified with `diff`, no output). The
`items` table is untouched in shape and kept as an unconditional
per-fetch observation log, exactly as before, with the "why" restated
(`store.py:5-7`) rather than silently repurposed. `--dry-run` still works —
I ran it directly and it prints the would-send body without touching the
`sent` table (`sent` had 0 rows after a dry run that would otherwise have
inserted 12). The new `sent` table is additive (`CREATE TABLE IF NOT EXISTS`),
so an existing on-disk DB with only the old `items` table upgrades cleanly —
I simulated this by hand-creating an old-schema DB and running `digest.py`
against it; it added the `sent` table without error or data loss.

### Code quality — 4 / 4

The diff (`store.py`, `digest.py`) is small, readable, and has no dead code
or vestigial columns. SQL is sensible: `INSERT OR IGNORE` on a composite
primary key (`store.py:33-39`) is the right idiom for "insert if absent,
otherwise no-op," and avoids a read-then-write race for the common case.

### Documentation — 3 / 4

`README.md:31-53` adds a full "Deduplication" section: identity choice
explained with a per-feed table of what's wrong with each provider's own
identifier, the per-channel scoping, and the retry-on-failure behavior. This
clears the "docstring alone" floor easily. It's not full marks because two
things the rubric explicitly asks for are missing: how to reset the dedupe
record (there's no `DELETE FROM sent` instruction or a `--reset` flag
mentioned anywhere), and no explicit statement of what happens on first run
(see Ambiguity #3 above) — a new deploy will silently blast every current
feed item to every channel on tick one, and nothing warns the reader.

## 3. What it missed

- **First-run backfill** is never named. A fresh deploy against non-trivial
  feeds will dump everything currently live to every channel in one go. Not
  addressed in code (no `--skip-existing` bootstrap option) or in the
  README.
- **No reset procedure documented.** If someone genuinely wants a channel
  to receive everything again (e.g., after a config change), the only path
  is manually deleting rows from `sent` via `sqlite3`, and this isn't
  written down anywhere.
- **Unbounded `items` growth** is inherited from the original design, kept
  intentionally, but the agent doesn't note that this log will now grow
  forever with no retention story — reasonable to leave alone given the
  task scope, but silent.
- **`channels.py`'s crash-aborts-the-loop behavior** (a `DeliveryError` on
  channel 2 stops channel 3+ from being attempted this tick) is inherited
  unchanged and not discussed, even though the new failure-mode reasoning in
  the README talks about single-channel retry as if channels were
  independent within a run.

## 4. Bugs

None found. I reproduced:

1. Same-snapshot re-run → 0 items sent the second time.
2. Cross-snapshot run (utm rotation + guid regeneration on unchanged links +
   one genuinely new item) → only the new item sent.
3. `--dry-run` → no `sent` table writes, output unaffected.
4. Upgrade of an old-schema DB (only `items` table) → `sent` table created
   without error, no data loss.

All behaved as documented; I did not find a way to make it double-send or
drop an item within the scope of the two fixtures.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 7,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 3,
  "total": 35,
  "would_merge": "yes",
  "one_line": "Solid link-based per-channel dedup, verified against fixtures; misses first-run and reset docs."
}
```
