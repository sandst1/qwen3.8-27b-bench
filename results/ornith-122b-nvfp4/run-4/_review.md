# Review: notify-digest dedup fix (ornith-122b-nvfp4, run-4)

## 1. Summary

The agent identified that every feed is re-fetched unchanged on every 15-minute
tick, added a `dedup_key` (link with the query string stripped) computed from
each item, and skips re-delivering anything whose key is already archived,
with the archive write deferred until after a channel successfully sends and
skipped entirely under `--dry-run`. The identity choice is well-argued and
genuinely survives all three feeds' quirks in the fixtures (rotating
`utm_campaign`, a regenerated `guid`, a feed with no id at all), and it ships
tests and a real migration for pre-existing databases. I would merge this
with one fix first: the archive write happens once at the end of `run_once`
for the whole batch, so if channel N raises mid-loop, items already delivered
to channels 1..N-1 are never archived and get **resent** on the next tick —
reproducing, in a narrower form, the exact bug the task was filed to kill.

## 2. Per-category scoring

### Identity strategy — 9.5 / 10

`store.dedup_key` (store.py:71-93):

```python
def dedup_key(item):
    """The identity we dedup on: the item's link with any query string stripped.
    ...
      * newsroom — `raw_id` (entry_id) is stable, but its link carries a
        rotating `utm_campaign` (w33 one tick, w34 the next) for the *same*
        article. Keying on the raw link would re-send it every tick.
      * wire — its link is stable, but `raw_id` (the guid) is regenerated
        whenever the story is edited (an `-r2` suffix, etc.). Keying on
        `raw_id` would re-send every edited story.
      * blogroll — has no `raw_id` at all.
    ...
    If a feed ever ships an item with no link, this returns "" and those items
    are effectively never deduped (each tick treats them as new) — acceptable,
    since every normaliser in feeds.py always sets a link.
    """
    return (item.get("link") or "").split("?", 1)[0]
```

This is almost a verbatim restatement of the rubric's top-band bar, and it
checks out against the fixtures, not just in prose:

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a again
sent 0 items
$ python3 digest.py --config config-b.toml --db digest.sqlite3 # snapshot-b
sent 2 items     # only the genuinely-new "Union responds..." article
```

- newsroom's `utm_campaign` rotates (`w33` → `w34`) on the *same* `port-fees`
  article; link-minus-query collapses both to one key.
- wire's `guid` is rewritten (`wire-2026-08-14-0031` →
  `...-0031-r2`) on edit; the link doesn't change, so identity holds.
- blogroll has no id of any kind; link is the only thing it can use, and it's
  stable across the retitle in `snapshot-b` ("Notes on port fee arithmetic"
  → "... (updated)").

The failure mode of the chosen identity itself is stated, not just the feeds'
failure modes (empty-link items are "effectively never deduped"). Not a full
10 only because it's one generic heuristic (URL path) rather than an explicit
per-feed fallback chain — if a future feed's canonical link also churns (e.g.
trailing slash, scheme change), nothing here would catch it, and that case
isn't flagged.

### Ambiguity handling — 5 / 8

Three forks exist; the agent names and handles one cleanly, documents a
second with an inaccuracy, and never raises the third.

**Edited-item-as-new-vs-same** — named and resolved explicitly, with
reasoning and tests (`test_digest.py:94-100`, `test_older_db_without_dedup_key_is_backfilled`).
Clearly top-of-band for this fork alone.

**Per-channel vs. global suppression** — resolved as global (one archive, no
per-channel state) and *partially* documented in README.md:53-54:

```
Dedupe means "don't re-deliver what we already **sent**". Items that matched no
channel are not archived, so adding a new channel later can still surface them.
```

That sentence is true only for items that matched *no* channel. I reproduced
the case it implies is fine but isn't — an item that already matched channel
`ops`, with a new channel `newteam` added later using the *same* keyword:

```python
# tick 1: channels = [ops]           -> ops gets 3 items, archived
# tick 2: channels = [ops, newteam]  -> sent: 0
```

`newteam` never receives items it has in fact never been sent, because the
archive is global per-link, not per-(channel, link). The README's framing
("adding a new channel later can still surface them") overstates what global
suppression actually guarantees.

**First-run backfill** — never named. The code's choice (empty archive ⇒
everything is "new" ⇒ full backlog goes out on the very first tick) is
correct and is exercised by `test_first_tick_sends`, but it is nowhere stated
as a decision — the README's "Upgrading" paragraph only covers the
*pre-existing-database* case, not a brand-new deploy.

One fork named well, one named but materially incomplete, one silent (but
correct) — solidly in the 4–6 band, at the top of it for the strength of the
edited-item treatment.

### Failure-mode reasoning — 3 / 8

Marking is **per-run**, not per-channel, despite a comment that reads as if
it were deliberate. `digest.py:59-89`:

```python
sent = 0
sent_items = []
for chan_cfg in cfg["channels"]:
    selected = [i for i in new_items if matches(i, chan_cfg)]
    if not selected:
        continue
    body = render.digest(selected, chan_cfg)
    if dry_run:
        print(f"--- would send to {chan_cfg['name']} ---")
        print(body)
        continue
    channels.send(chan_cfg, body)
    sent += len(selected)
    sent_items.extend(selected)

# Archive only what we actually delivered, and only after delivery, so a
# failed run re-delivers on the next tick (cron retries) instead of marking
# items seen without sending them. ...
if not dry_run:
    by_source = {}
    written = set()
    for item in sent_items:
        key = store.dedup_key(item)
        if key in written:
            continue
        written.add(key)
        by_source.setdefault(item["source"], []).append(item)
    for source, items in by_source.items():
        store.record_items(db, source, items)
```

`channels.send` is allowed to raise (channels.py's own long-standing
contract: "if a channel is down we let the exception propagate and cron will
pick us up again"). But the archive write for *every* channel's items sits
after the loop, so if channel 2 raises, the `for source, items in
by_source.items()` block is never reached at all — not even for channel 1,
which already succeeded. Reproduced:

```
tick 1: channels = [ops, broken-webhook]
  ops sends 3 items successfully, prints them
  broken webhook raises DeliveryError -> run_once propagates, nothing archived

tick 2 (cron retry, ops-only config, same feed snapshot):
  sent on retry: 3   <-- ops gets the same 3 items a second time
```

That is exactly the "same items over and over" complaint the task describes,
now triggered by any transient failure of a *later* channel rather than by
every tick. The in-loop comment argues one side of the tradeoff (avoid
marking-without-sending) but never acknowledges the other side (re-sending to
channels that already got it) — so there is *some* discussion, which keeps
this out of the 0–1 band, but the marking is per-run, not per-channel, and a
partial-channel failure duplicates a whole sub-batch. That is squarely the
"partial-failure window that loses or duplicates a whole batch" line in the
rubric's 2–3 band; I put it at the top of that band for the (incomplete)
discussion that does exist.

### Existing-code respect — 6 / 6

`feeds.py`, `channels.py`, `render.py`, `config.example.toml` are byte-for-byte
unchanged. `digest.py` and `store.py` are edited additively, not rewritten.
Critically, the agent found and fixed the exact landmine the rubric calls
out — the original unconditionally archived on every tick, including during
`--dry-run`:

```python
# original digest.py
for feed_cfg in cfg["feeds"]:
    ...
    store.record_items(db, feed_cfg["name"], items)   # ran even with --dry-run
    all_items.extend(items)
```

The fix moves archiving to after delivery and gates it on `not dry_run`
(digest.py:79), with a dedicated regression test
(`test_dry_run_does_not_burn_the_backlog`, test_digest.py:102-109) proving a
dry run computes but never sends or archives. `--dry-run` output format is
unchanged. No scope creep (the added `test_digest.py` is in-scope proof of
the fix, not a new feature).

### Code quality — 3.5 / 4

Clean, commented, and the schema migration is handled properly
(`store.py:47-68`): `ALTER TABLE ... ADD COLUMN`, backfill via SQL that
mirrors the Python `dedup_key` logic, index created only after the column
exists, idempotent (`CREATE INDEX IF NOT EXISTS`). Covered by
`MigrationTest.test_older_db_without_dedup_key_is_backfilled`.

Two small nits:
- `digest.py:36-46` builds `fetched = [(name, items), ...]` purely to throw
  the name away immediately (`all_items = [i for _, items in fetched for i in
  items]`) — `item["source"]` is already set by `feeds.fetch`, so this list is
  unused indirection; the original's direct `all_items.extend(items)` would
  have done the same job.
- `store.seen_keys` (store.py:96-106) builds a `?`-per-key `IN (...)` clause
  sized to the tick's item count; fine at this feed count, but there's no
  comment noting the implicit ceiling (SQLite's default ~999 bound
  parameters) if feeds ever grow large.

Neither is a functional bug, hence 3.5 rather than lower.

### Documentation — 3 / 4

Real prose, not just a docstring — README.md gets a dedicated
"## Deduplication" section (README.md:31-58) covering the identity choice,
the per-feed reasons `raw_id` doesn't work, the archive-after-delivery
timing, and the migration path for existing databases. That clears the
"docstring alone caps this at 2" floor comfortably.

Two of the rubric's three explicit asks are only partly there:
- **How to reset it** — absent. There's no "delete `digest.sqlite3`" or
  equivalent instruction anywhere for someone who wants to force a replay.
- **What happens on first run** — covered only for the *upgrade* case
  ("first run after deploy does not re-deliver the existing archive"); a
  brand-new install's first tick (which sends the entire current backlog at
  once) is never mentioned, despite being exercised by
  `test_first_tick_sends`.

## 3. What it missed

- **Per-channel archive timing.** Nothing requires that items are archived
  immediately after each channel's `channels.send` succeeds; archiving is
  batched to the end of `run_once`, so a later channel's crash erases the
  record of earlier, successful deliveries in the same tick (see Bugs below).
- **Global suppression's edge case.** The README asserts unmatched items can
  "still surface" when a channel is added later, but doesn't mention that an
  item already matched by *some* channel is suppressed for every channel
  forever after, including ones added later that never got it.
- **First-run behavior as a named decision.** The full-backlog-on-first-tick
  choice is correct and tested but never stated as a choice anywhere in
  README or comments.
- **How to reset/replay.** No documented way to intentionally re-deliver
  (truncate `items`, drop `dedup_key`, etc.) if someone needs to.
- **`raw_id` is still written to every row** (store.py:111-117) despite
  being dead for identity purposes, same as before the change — not a new
  issue, but a missed opportunity to either use it or note it's archival-only.

## 4. Bugs

**Partial-channel failure causes duplicate delivery to channels that already
succeeded in the same tick.** Reproduction (ran against the actual fixtures):

```python
import store, digest

cfg = {
    "feeds": [
        {"name": "newsroom", "format": "newsroom", "url": "file://fixtures/snapshot-a/newsroom.json"},
        {"name": "blogroll", "format": "blogroll", "url": "file://fixtures/snapshot-a/blogroll.json"},
        {"name": "wire", "format": "generic", "url": "file://fixtures/snapshot-a/wire.json"},
    ],
    "channels": [
        {"name": "ops", "type": "stdout", "title": "Ops digest", "keywords": ["port"]},
        {"name": "broken", "type": "webhook", "url": "http://localhost:1/doesnotexist", "keywords": []},
    ],
}

db = store.connect("digest.sqlite3")
try:
    digest.run_once(cfg, db, dry_run=False)   # ops prints 3 items, then "broken" raises
except Exception as e:
    print("raised:", e)

# cron retries 15 min later; webhook still down, so ops-only config this time
sent = digest.run_once({**cfg, "channels": [cfg["channels"][0]]}, db, dry_run=False)
print("sent on retry:", sent)   # -> 3, the SAME 3 items ops already received
```

Output:
```
=== ops ===
...3 items printed...
raised: broken: <urlopen error [Errno 61] Connection refused>
sent on retry: 3
```

`ops` gets its 3 items twice. This isn't hypothetical: `channels.py`'s own
docstring says delivery is "best effort" and failures are expected to
propagate so cron retries — the new dedup code doesn't account for that
existing contract when it defers all archiving to the end of the channel
loop.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 5,
  "failure_mode_reasoning": 3,
  "existing_code_respect": 6,
  "code_quality": 3.5,
  "documentation": 3,
  "total": 30,
  "would_merge": "yes-with-fixes",
  "one_line": "Strong link-based dedupe, well tested, but re-sends a batch on any mid-run channel failure."
}
```
