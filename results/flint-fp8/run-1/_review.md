# Review: notify-digest dedup fix (flint-fp8, run-1)

## 1. Summary

The agent built a "sent ledger": `digest.py` now looks up an item's identity
in the existing `items` table *before* adding it to the send pool, and only
writes that row *after* a channel's `channels.send()` call returns
successfully, with identity defined as `(source, link-without-querystring)`
in `store.py`. This correctly suppresses the three concrete duplication
patterns the fixtures were built to test (rotating `utm_campaign`, a missing
`raw_id`, a regenerated `guid`), verified by running both snapshots through
the tool. I would merge this **with fixes**: the single-channel, no-failure
path is solid and well-documented, but the suppression is global across
channels rather than per-channel, and that is not just a documented
simplification — it is a reproducible bug that permanently drops an item for
a channel whose delivery failed, if any other channel's delivery for the same
item succeeded first in the same run.

## 2. Per-category scoring

### Identity strategy — 8.5 / 10

`store.py:51-59`:

```python
def item_key(source, raw_id, link):
    """Stable identity for an item: (source, canonical link).

    The query string is stripped from the link so that tracking params (e.g.
    utm_*) or feed-specific suffixes don't make the same item look new on every
    fetch. Falls back to the raw id when there is no link.
    """
    identity = (link or "").split("?", 1)[0] or (raw_id or "")
    return (source, identity)
```

I ran both fixture snapshots through the modified tool (Python 3.13,
`tomllib` requires 3.11+; the box's default `python3` is 3.9 and can't run
this at all — not the agent's fault, just a note for whoever runs this).

Run 1 on `snapshot-a` sends 12 items (3 channels × items). Run 2, same
snapshot again (simulating two cron ticks with no feed change): `sent 0
items`. Run 3 on `snapshot-b`:

```
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
=== everything ===
Firehose — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```

That's exactly right: the only genuinely new item (`entry_id 84130`) goes
out; the `newsroom` items whose link gained a new `utm_campaign=w34` are
suppressed; the `wire` item whose `guid` was regenerated
(`wire-2026-08-14-0031` → `...-r2`) is suppressed because its `link` didn't
change; the `blogroll` item that was silently edited ("(updated)" title, new
excerpt) is suppressed for the same reason. All three fixture traps are
handled, and the rationale is stated in the docstring above and restated in
the README (quoted under Documentation below).

Docked 1.5 points for two things worth a sharp eye:
- `raw_id` is now dead code for every feed currently defined: `feeds.py`
  always sets `link` to a required field for all three formats
  (`r["url"]`, `r["permalink"]`, `r["link"]`), so `(link or "").split(...)`
  is always truthy and the `or (raw_id or "")` branch never executes. The
  "fallback chain" the docstring describes isn't actually exercised by
  anything in this codebase — it reads as designed-for-the-fixture rather
  than designed-for-the-general-case.
- The strategy itself has an unstated failure mode: two genuinely different
  items that differ *only* by query string (a real pattern on some sites,
  e.g. `?id=`) would collide under this rule. Not a problem for these
  fixtures, but the choice is "stated with its rationale," not "stated with
  its failure modes" as the rubric's top band asks for.

### Ambiguity handling — 3 / 8

Of the three forks the rubric names, one is addressed, one is implicit, one
is both unaddressed and gotten wrong.

- **Edited item as new vs. same** — named and resolved, in `README.md:39-41`:
  > "An item's identity is `(source, canonical link)`; the query string is
  > stripped from the link so tracking params (`utm_*`) or regenerated guids
  > on edited items don't make the same item look new on every fetch."
  This is a real, stated decision ("edits don't count as new"), and it's
  correct for this ticket's framing (stop re-notifying people).

- **First-run backfill** — never mentioned. A fresh `digest.sqlite3` means
  `get_seen()` returns an empty set for every source, so the very first cron
  tick after deploying this sends *everything currently in the feed* to
  every matching channel at once. That's a reasonable default, but it's a
  real operational event (a backfill blast on rollout) and the README is
  silent on it.

- **Per-channel vs. global suppression** — decided silently, and decided
  wrong. See the Bugs section: suppression is keyed by `(source, link)`
  only, with no channel dimension, so once *any* channel successfully
  delivers an item, every other channel — including ones that have not yet
  received it, including ones added to the config later — can never receive
  it. This is precisely the rubric's example of the wrong default
  ("typically global suppression").

One fork handled well lifts this off the floor, but the global-suppression
default being both silent and wrong caps it in the 2-3 band.

### Failure-mode reasoning — 3 / 8

`digest.py:52-64`:

```python
for chan_cfg in cfg["channels"]:
    selected = [i for i in all_items if matches(i, chan_cfg)]
    if not selected:
        continue
    body = render.digest(selected, chan_cfg)
    if dry_run:
        print(f"--- would send to {chan_cfg['name']} ---")
        print(body)
        continue
    channels.send(chan_cfg, body)
    for i in selected:
        store.record_items(db, i["source"], [i])
    sent += len(selected)
```

The ordering — mark sent only *after* `channels.send()` returns — is the
right call for an at-least-once guarantee, and better than marking
up-front. But the marking is **not per-channel**: `record_items` writes to
the same `items` table that `get_seen(db, source)` reads from, keyed only by
`(source, link)`, with no channel field anywhere in the schema or the
write. Combined with `channels.send` raising an uncaught exception that
aborts the whole `run_once` call (unchanged from the original — see
`channels.py:5`, "we let the exception propagate"), this produces a real
data-loss window:

Reproduction (full transcript in the exploration, condensed here):
config with two channels, `first` (stdout, catches one item) then `second`
(webhook to a closed port, catches everything):

```
=== first ===
* Regulator opens inquiry into port fees  [newsroom]
channels.DeliveryError: second: <urlopen error [Errno 61] Connection refused>
exit: 1
```

`first` succeeded and `store.record_items` ran for its item *before*
`second`'s send raised. Fix "second" (point it at stdout) and retry the
identical run:

```
=== second ===
* Grid operator delays offshore tender  [newsroom]
sent 1 items
```

"Regulator opens inquiry into port fees" is gone. `second` never received
it — its only send attempt crashed the process — but because `first`
already marked that item's `(source, link)` as seen before the crash, the
retry's feed-fetch-time filter (`digest.py:40-47`) drops it before the
channel loop even runs. It is lost forever, not just delayed. This is
exactly the rubric's "partial-failure window that loses ... a whole batch"
case, just scoped to one channel instead of all of them. The README's claim
— "a failed delivery is retried on the next cron tick" — is true only when
no other channel already consumed the item in the same run; it's incomplete
in the multi-channel case, and that incompleteness isn't flagged anywhere.

### Existing-code respect — 5.5 / 6

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are
byte-identical to the original (verified with `diff`). The change is
contained to `digest.py`, `store.py`, and `README.md`. The rubric explicitly
calls out that reusing the `items` archive is fine *if the unconditional
insert is dealt with* — the original unconditionally called
`store.record_items(...)` for every fetched item before any filtering
(`digest.py:40` in the original); the agent moved that call to fire only
after a successful send, which is the right fix for exactly the problem the
rubric names. `--dry-run` still works and, confirmed by test, writes nothing
to the database on two consecutive dry runs. No schema migration was needed
because the agent added no columns — it reused the existing `raw_id`/`link`
columns as-is.

Docked half a point: the per-channel loop in `digest.py:62-63` calls
`store.record_items` once per *selected item per channel*, so an item
matching two channels (which happens constantly here, since the
`everything` channel matches all items) gets written to the archive twice.
That's a byproduct of bolting per-send marking onto a loop structure that
was never per-item before, and it compounds the global-suppression problem
by writing to the shared table from multiple call sites in the same run.

### Code quality — 2.5 / 4

The diff itself is small and readable, and the new functions are short and
named clearly. Issues:

- Verified duplicate rows: running the three-channel example config once
  against `snapshot-a` leaves **14 rows for 6 items** in `items` — every
  item that matched more than one channel (which is most of them, because
  of the catch-all `everything` channel) was inserted twice. No unique
  constraint exists to catch this at the DB layer either.
- `get_seen` (`store.py:62-67`) does `SELECT ... WHERE source = ?` with no
  bound on time or row count, then rebuilds the whole seen-set in Python on
  every single run, forever. For a cron job running every 15 minutes with no
  pruning and now double-writing rows, this table has no bounded growth
  story and nothing in the code or docs mentions one.
- `raw_id` is retained as a parameter and a fallback branch in `item_key`
  that, as shown above, is unreachable given the current `feeds.py`. It's
  not wrong, but it reads as untested/aspirational rather than exercised.

### Documentation — 2 / 4

`README.md` gets a real "## Deduplication" section (not just a docstring),
which keeps it off the bottom of this category:

```
Each run only sends items that have not been sent before. The SQLite archive
(`--db`) is the source of truth: rows are written only after a digest has
been delivered successfully, and the next run reads them back
(`store.get_seen`) to filter out what it has already sent. Nothing is marked
as seen when a send fails, so a failed delivery is retried on the next cron
tick.

An item's identity is `(source, canonical link)`; the query string is
stripped from the link so tracking params (`utm_*`) or regenerated guids on
edited items don't make the same item look new on every fetch.
```

This is clear prose and explains the identity choice. But the rubric asks
for three specific things and only one is really covered:
- *How to reset it* — not mentioned at all (e.g. "delete `digest.sqlite3`"
  or "an item can be forced to resend by deleting its row"). There is no
  operational escape hatch documented.
- *What happens on first run* — not mentioned; see the backfill note above.
- And, as shown in Failure-mode reasoning, the one claim that *is* made
  about retry behavior is incomplete for the multi-channel case, which the
  README doesn't flag as a known limitation.

## 3. What it missed

- **Per-channel suppression.** Never named as a decision; the global
  `(source, link)` ledger means a channel added later, or a channel whose
  keyword filter starts matching an item after the fact, can never receive
  an item that any other channel already got. Silent, and wrong per the
  rubric's own example.
- **First-run backfill.** A fresh database sends everything currently in
  every feed to every matching channel on the very first tick. Not
  discussed, not gated behind a flag, not mentioned in the README.
- **The interaction between partial multi-channel failure and global
  marking.** Not just undocumented — actively contradicts the README's own
  claim that failed deliveries are retried (true only when nothing else
  succeeded for that item in the same run).
- **Database growth / pruning.** No discussion of what happens to
  `digest.sqlite3` over months of 15-minute ticks, now with roughly 2×
  the insert rate from the per-channel-per-item write.
- **How to reset/replay.** No operator guidance for "I want this item
  resent" or "wipe the dedup state."

## 4. Bugs

**Channel-partial-failure item loss (reproducible).** Config with two
channels; first one succeeds and is cheap (stdout), second one fails
(webhook to a closed port):

```toml
[[feeds]]
name = "newsroom"
format = "newsroom"
url = "file://fixtures/snapshot-a/newsroom.json"

[[channels]]
name = "first"
type = "stdout"
keywords = ["port"]

[[channels]]
name = "second"
type = "webhook"
url = "http://127.0.0.1:1/nope"
keywords = []
```

```
$ python3 digest.py --config config-fail.toml --db fail.sqlite3
=== first ===
* Regulator opens inquiry into port fees  [newsroom]
...
channels.DeliveryError: second: <urlopen error [Errno 61] Connection refused>
$ echo $?
1
```

Point `second` at `stdout` (simulating the channel recovering) and run the
*identical* feed snapshot again:

```
$ python3 digest.py --config config-fail-recovered.toml --db fail.sqlite3
=== second ===
* Grid operator delays offshore tender  [newsroom]
sent 1 items
```

"Regulator opens inquiry into port fees" — the item `second` never actually
received — is gone from its digest permanently. It was marked seen by
`first`'s successful send before `second`'s send crashed the process, and
the feed-fetch-time filter in `digest.py:40-47` removes it from the pool on
every subsequent run regardless of channel. No error, no warning — the item
simply never appears again.

**Duplicate archive rows (minor, functional but wasteful).** Any item
matching more than one channel is written to `items` once per matching
channel in the same run. Confirmed by inspecting `digest.sqlite3` after one
run of the example three-channel config against `snapshot-a`: 6 distinct
items produced 14 rows (most items matched both their topic channel and the
catch-all `everything` channel).

```json
{
  "identity_strategy": 8.5,
  "ambiguity_handling": 3,
  "failure_mode_reasoning": 3,
  "existing_code_respect": 5.5,
  "code_quality": 2.5,
  "documentation": 2,
  "total": 24.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Good link-based identity; global cross-channel suppression silently drops items on partial failure."
}
```
