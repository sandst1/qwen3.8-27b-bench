# Review: notify-digest dedup fix (claude-sonnet-5, run-1)

## 1. Summary

The agent identified that duplicates come from the lack of a sent-item ledger, added a
`sent(channel, source, raw_id)` table checked before delivery and written after a
successful `channels.send`, and — critically — fixed the per-feed `raw_id` derivation so
the key is actually stable across re-fetches (permalink for blogroll, `link` instead of
`guid` for wire, `entry_id` instead of URL for newsroom). I verified this against both
fixture snapshots: re-running the same snapshot sends nothing on the second pass, and
moving from snapshot-a to snapshot-b correctly suppresses the edited/re-guid'd/re-utm'd
items while still delivering the one genuinely new item. I would merge this with minor
follow-up (the `items` archive table's unconditional insert was left unaddressed, and
first-run/reset behavior is undocumented), not block it.

## 2. Per-category scoring

### Identity strategy — 9/10

The agent didn't pick one dedupe key for all three feeds; it worked out, per format, which
field is actually invariant across re-fetches of the same logical item, and says so in
three places (`feeds.py`, `store.py`, `README.md`). From `feeds.py`:

```python
if fmt == "blogroll":
    # No stable identifier of any kind in this one, but `permalink`
    # doesn't change when a post is edited in place (title/excerpt do),
    # so we use it as the dedup key.
    ...
    "raw_id": r["permalink"],

# "generic": has a guid, but the provider regenerates it whenever an
# item is edited (typos, added tags, retitles) — e.g. "...-0031" becomes
# "...-0031-r2" for the same story. `link` is stable across those edits,
# so we use it as the dedup key instead of the guid.
...
"raw_id": r["link"],
```

and for newsroom, `"raw_id": str(r["entry_id"])` instead of the URL (which carries a
rotating `utm_campaign`).

I confirmed this against the fixtures directly. Running snapshot-a, then snapshot-b (which
regenerates the wire guid `wire-2026-08-14-0031` → `wire-2026-08-14-0031-r2`, bumps the
newsroom `utm_campaign` from `w33` to `w34` on every entry, and edits the blogroll excerpt
in place) correctly suppresses all three edited items and delivers only the one truly new
item (`port-fees-union`):

```
=== RUN 3 (snapshot-b) ===
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
=== everything ===
Firehose — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```

This is exactly the top-band description: survives rotating `utm_campaign`, a regenerated
`guid`, and (for blogroll) a provider with no id field at all. Not a full 10 because there's
no fallback chain if a provider's chosen stable field is itself absent on some entries
(e.g. a future blogroll post missing `permalink`) — `r["permalink"]` / `r["link"]` /
`r["entry_id"]` are all unconditional subscripts, so a malformed entry throws instead of
degrading. Minor, but it's the difference between "stated failure modes" and "handles
failure modes."

### Ambiguity handling — 6/8

Two of the three canonical forks are named and resolved explicitly; the third isn't
addressed at all.

- **Per-channel vs. global suppression** — named and justified in `store.py`:

  ```python
  Dedup key: (channel, source, raw_id). It's per-channel (not just per-item)
  because a single item can legitimately go out to more than one channel
  (e.g. an "everything" firehose channel alongside a keyword-filtered one) —
  those are independent deliveries and each needs its own "have we sent this"
  answer.
  ```

  Correct choice, correctly implemented (`sent` PK is `(channel, source, raw_id)`), and
  explained.

- **Edited item as new vs. same** — named and resolved via the `raw_id` rationale in
  `README.md` and `feeds.py` (quoted above): an edit does not regenerate the dedupe key,
  so edited items are treated as "the same item," not a new notification. This is a real
  decision, stated with reasoning, not a hidden default.

- **First-run backfill** — never mentioned anywhere. On a brand-new `digest.sqlite3`, the
  `sent` table is empty, so the *entire* current content of every feed that matches a
  channel's keywords goes out in the very first run — no matter how far back items go. I
  confirmed this is exactly what happens (first run against snapshot-a delivered all 6
  matching items across all three channels with no distinction from a normal run). This may
  well be the right default, but it's a real fork ("do we hydrate history silently and
  spam a channel the day it's first configured, or backfill nothing / require an explicit
  flag?") that is neither named nor discussed anywhere in the README or code.

Two of three forks explicitly named and correctly resolved, one undiscussed but not
wrong — solidly mid-band, at the top of it.

### Failure-mode reasoning — 5/8

Marking is per-channel and ordered correctly relative to delivery — mark only happens
after `channels.send` returns without raising, inside the per-channel loop:

```python
channels.send(chan_cfg, body)
store.record_sent(db, chan_name, selected)
sent += len(selected)
```

I verified the partial-failure case directly by monkeypatching `channels.send` to raise on
the second channel (`energy`) while letting the first (`ops`) succeed, then re-running
`run_once`:

```
caught expected crash: simulated outage
{'channel': 'ops', 'count(*)': 3}
```

`ops`, processed before the failure, is durably marked and won't be redelivered on the
next tick; `energy`, which raised, is not marked and will correctly retry. This is sane,
at-least-once-per-channel behavior, and it happens to be right — but nothing in the code
or docs says so. There's no comment in `digest.py` or `README.md` about what happens when
channel two fails after channel one succeeds, no mention of at-least-once vs.
at-most-once, and no note that an uncaught `DeliveryError` from `channels.send` will crash
the whole cron invocation (skipping all channels *after* the failing one on that tick, even
if they had nothing to do with the outage). That's a real omission for a cron job whose
whole failure surface is "the webhook is down." Matches "per-channel marking, sane
ordering, no discussion" almost exactly.

### Existing-code respect — 4.5/6

The diff is minimal and surgical — `channels.py` and `render.py` are untouched,
`digest.py`'s per-run loop structure is preserved, `--dry-run` still works correctly (I
verified: two dry-runs in a row against the same db both report all items, then a real run
right after still sends everything — dry-run genuinely never touches the ledger, matching
the README's claim). The new `sent` table is added via `CREATE TABLE IF NOT EXISTS`
alongside the existing schema, so it migrates cleanly onto an existing `digest.sqlite3`
with no explicit migration code needed.

However, the rubric calls out one thing specifically: "Reusing the `items` archive is fine
*if* the unconditional insert is dealt with." It wasn't. `store.record_items` is still
called unconditionally every run for every fetched item, with no uniqueness constraint on
`(source, raw_id)`:

```python
def record_items(conn, source, items):
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ...
    )
```

I confirmed the effect directly — running the same snapshot through `digest.py` twice
against a fresh db:

```
total items rows: 12
{'source': 'wire', 'raw_id': 'https://wire.example/i/0031', 'c': 2}
{'source': 'wire', 'raw_id': 'https://wire.example/i/0918', 'c': 2}
... (same story for every source/raw_id pair)
```

At a 15-minute cron cadence this table grows by a full copy of every still-live item on
every tick, forever — the exact "unconditional insert" the rubric flags as needing to be
dealt with when reusing this table. The agent touched `store.py` extensively for the new
`sent` table and evidently read `record_items`, but didn't flag or fix this.

### Code quality — 3.5/4

Clean, readable, idiomatic — parametrized SQL throughout (no injection risk in the
dynamic `IN ((?,?),(?,?)...)` construction in `already_sent`), no dead code, consistent
naming, docstrings that carry real information rather than boilerplate. Docked half a
point for the same reason as above: the `items` table is effectively becoming a
write-only, ever-growing duplicate log with no pruning or a unique index that a competent
reviewer would flag as "sensible SQL for what this table is now being asked to do."

### Documentation — 3/4

Goes well beyond a docstring: `README.md` gets a dedicated "## Dedup" section explaining
*why* dedup is needed (re-fetch/re-filter every run, no since-last-run fetch), what
mechanism prevents it (`sent` table, checked/updated relative to send), and a per-feed
breakdown of why each `raw_id` choice was made — this is genuinely useful for the next
person and matches the rubric's ask for "explain the dedupe behaviour." It falls short of
the full ask on two of the three named items: it does not explain **how to reset**
dedup state (e.g. "delete the `sent` table / drop `digest.sqlite3`" or "there's no CLI flag
for it") and does not discuss **first-run** behavior (that a fresh db will backfill and
deliver everything currently matching, all at once). Both are real operational questions
someone will hit.

## 3. What it missed

- **First-run backfill** is a silent default (deliver everything current in the feed) —
  reasonable, but never named or discussed (see Ambiguity handling above).
- **The `items` archive's unconditional insert** grows without bound and now duplicates
  fully on every tick; the rubric explicitly calls this out and the agent left it alone
  despite editing `store.py` around it.
- **Crash/retry semantics are not discussed anywhere**, even though the empirically
  correct behavior (per-channel at-least-once) is exactly what a reasoning-through-it
  fix would produce. No note on what happens to channels queued *after* a failing one in
  the same tick (they're simply never attempted that run, and will be attempted fresh
  next tick with no distinction between "not yet tried" and "we got to it but it failed").
- **How to reset dedup state** is not documented (no flag, no README mention of deleting
  the `sent` table or the db file).
- **No test coverage added** — the whole verification here was done by hand against the
  fixtures; there's nothing in the repo that would catch a regression to, say, the wire
  `raw_id` choice reverting to `guid`.

## 4. Bugs

None found that break correctness of the stated feature. Specifically:

- Dedup across repeated runs of the same snapshot: verified empty second run.
- Dedup across snapshot-a → snapshot-b (edited items, rotated `utm_campaign`, regenerated
  `guid`): verified correct suppression, only the new item delivered.
- `--dry-run` never consumes the ledger: verified two dry-runs followed by a real run all
  saw the same 12-item set.
- Partial-channel-failure ordering: verified the succeeding channel is marked and the
  failing one is not, via a monkeypatched `channels.send`.

The one thing that will bite someone in production is not a crash, it's the `items` table
growth described above (unbounded duplication of every fetched item, every 15 minutes,
forever) — not "broken" in the sense of wrong output, but a slow-motion disk/maintenance
problem that the rubric specifically warns reviewers to check for.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 5,
  "existing_code_respect": 4.5,
  "code_quality": 3.5,
  "documentation": 3,
  "total": 31,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid per-feed dedupe keys, verified correct; items-archive bloat and reset/backfill docs left unaddressed."
}
```
