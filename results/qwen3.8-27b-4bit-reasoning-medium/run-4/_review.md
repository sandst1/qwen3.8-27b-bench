# Review: notify-digest dedup fix (run-4)

## 1. Summary

The agent added a `sent` table keyed on `(channel, source, canonical_link)` to
suppress items already delivered to a given channel, normalizing links by
stripping `utm_*` and a short list of tracking params so that feed-specific
identifier weaknesses (rotating campaign tags, missing ids, regenerated
guids) don't defeat dedup; it also made the `items` archive idempotent via a
`UNIQUE(source, norm_link)` constraint and `INSERT OR IGNORE`. I verified
against both fixture snapshots that repeats are suppressed, edits are treated
as the same item, and genuinely new items still get through, and the code
respects `--dry-run` and existing files. I would merge this with fixes: the
one serious defect is that it does nothing for a pre-existing `digest.sqlite3`
from before this change, and will crash the very first cron tick after
deploy — a one-line `ALTER TABLE ... ADD COLUMN` migration was needed and
wasn't written.

## 2. Per-category scoring

### Identity strategy — 9/10

The docstring in `store.py:9-32` states the strategy and explicitly walks
through why each feed's own identifier fails:

```python
"""
Identity / the dedup key
------------------------
Each feed format is only half-reliable about identifiers:

* newsroom  -> a stable `entry_id`, but the URL carries rotating `utm_*` params
* blogroll  -> no identifier at all, just a stable permalink
* wire      -> a `guid` that is regenerated whenever the item is edited

So neither `raw_id` nor the raw `link` is a dependable key on its own...
We therefore identify an item by **(source, canonical link)**...

Trade-off: if a provider ever *moves* an article to a genuinely new URL, we
treat it as new and resend it.
"""
```

The implementation (`store.py:63-76`):

```python
def normalize_link(link):
    """Strip tracking query params so the same article yields the same key."""
    parts = urllib.parse.urlsplit(link)
    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    query = urllib.parse.urlencode(kept)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def identity(item):
    """(source, canonical link) — the stable identity we dedup on."""
    return item["source"], normalize_link(item["link"])
```

I ran this against both fixtures. First run on `snapshot-a` sends 12 items
across 3 channels. Second run on the same snapshot sends 0 (repeat
suppression works). Switching the config to `snapshot-b` (same db) sends
exactly 1 item — the genuinely new union-response article — even though:

- newsroom's `entry_id` for the port-fees article changed only in that its
  `utm_campaign` rotated `w33 -> w34` (correctly *not* re-sent)
- wire's `guid` for the port-fee-inquiry article changed from
  `wire-2026-08-14-0031` to `...-r2` because the item was edited (correctly
  *not* re-sent)
- blogroll's title changed to "Notes on port fee arithmetic (updated)" with
  no id at all (correctly *not* re-sent, since link is unchanged)

That is a real, fixture-verified win over `raw_id` or raw-link dedup, and the
failure mode (moved-URL = new item) is named rather than hidden. Docked one
point because the strategy is single-track (canonical link only) with no
fallback to `raw_id` when a link genuinely does move but the item is
otherwise identifiable — a corner case the docstring flags but doesn't try to
solve, which is a defensible but not maximal choice.

### Ambiguity handling — 5.5/8

Per-channel vs. global suppression is named and resolved explicitly, in both
code and prose. `digest.py:57-61`:

```python
# We run from cron every few minutes and feeds keep re-serving old
# items, so only deliver what is new *to this channel*. Dedup is per
# channel because each channel has its own filter: an item can be old
# to one channel and new to another. See store.py for why we key on
# (source, canonical link) rather than raw_id or the raw link.
```

and in README.md: "Dedup is per channel, not global, because each channel has
its own keyword filter: the same item can be old to one channel and new to
another." This is correct and well-argued — top marks for this one fork.

Edited-item-as-new-vs-same is *decided* correctly (verified above: guid
rotation and title edits don't trigger a resend) but it is never named as its
own decision. The docstring only frames it as a consequence of the identity
choice ("a `guid` that is regenerated whenever the item is edited" as a
problem to route around), not as an explicit "we consider an edited item the
same item, here's why" statement.

First-run backfill is not addressed anywhere — not in the README, not in a
comment, not in `digest.py`'s `main()`. On an empty db every item currently
matching a channel's filter gets sent, which is a reasonable default, but
nothing acknowledges that this is a choice (vs., say, seeding the archive
without sending on the very first run). Grep confirms: no occurrence of
"first" or "backfill" anywhere in the diff.

One fork named and argued well, one decided correctly but not surfaced as a
named decision, one never mentioned — mid-band.

### Failure-mode reasoning — 7.5/8

Marking is per-channel, and the ordering is send-then-mark:

```python
channels.send(chan_cfg, body)
# Record the send only after the channel accepts it (at-least-once).
store.mark_sent(db, chan_cfg["name"], fresh)
sent += len(fresh)
```

If channel 2 fails after channel 1 succeeded, channel 1's `mark_sent` has
already committed (`store.py`'s `mark_sent` calls `conn.commit()` itself), so
a retry on the next cron tick will not double-send to channel 1 and will
still attempt channel 2. The at-least-once tradeoff is explicitly argued, not
just implemented, in both `store.py`'s docstring and the README: "Delivery is
at-least-once: we record a send only *after* the channel accepts it... For a
digest, a rare duplicate is far better than a missed item." That is exactly
what the top band asks for. Half a point off because it doesn't address what
happens if the *db write itself* fails after a successful send (a much
smaller residual risk than the case it does discuss, but silent).

### Existing-code respect — 4.5/6

`feeds.py`, `channels.py`, and `render.py` are untouched. `--dry-run` is
verified to still work and, importantly, verified to *not* consume dedup
state — I ran `--dry-run` twice in a row and got the same "would send"
output both times, because `mark_sent` is skipped on the dry-run path:

```python
if dry_run:
    print(f"--- would send to {chan_cfg['name']} ---")
    print(body)
    continue
```

The unconditional-insert problem in the `items` archive is handled
(`UNIQUE(source, norm_link)` + `INSERT OR IGNORE`), per the rubric's explicit
callout that reuse is fine "if the unconditional insert is dealt with."

The deduction is for a genuine breakage: the task is framed as "we run this
from cron" — i.e., a live job with a live `digest.sqlite3`. I reproduced this
directly: build a db with the *original* schema (no `norm_link` column, no
`sent` table), then run the *new* `digest.py`/`store.py` against it:

```
sqlite3.OperationalError: table items has no column named norm_link
```

`store.py`'s `SCHEMA` is `CREATE TABLE IF NOT EXISTS`, which is a no-op
against an existing `items` table — there is no `ALTER TABLE` or version
check. Deploying this fix onto the actual box described in the prompt would
crash the very first cron tick, which is precisely the kind of bulldozing
this category penalizes (it doesn't rewrite files, but it does silently
assume a fresh db).

### Code quality — 2/4

The new code is clean and consistently styled with the original — clear
function names (`normalize_link`, `identity`, `sent_keys`, `mark_sent`),
sensible SQL (`INSERT OR IGNORE`, composite primary key on `sent`, an index
carried over from the original), and no dead code. The `_TRACKING_PARAMS`
set and `utm_` check are simple and legible.

The docked points are for the same migration gap as above, scored here too
because the rubric puts "schema migration handled for an existing database"
squarely in this category's checklist, and because of one smaller nit: on a
repeat run where a blogroll item's title/excerpt changes but the link
doesn't, `INSERT OR IGNORE` means the *archive* copy is never updated — the
`items` table permanently keeps the first-seen title/summary even after the
provider edits the piece. Harmless for the current "did this ever come
through" use case, but it's an unremarked staleness that a future reader of
the archive could be surprised by.

### Documentation — 3/4

README gained a dedicated section, "How it avoids re-sending the same
items," which explains the mechanism, the per-channel-not-global decision,
the identity choice with each feed's specific reason, and the at-least-once
tradeoff — clearly more than a docstring-only explanation, so it clears the
2-point cap. It also has an explicit operational warning: "Keep it in place
and don't delete it or point cron at a fresh file, or it will resend
everything."

It falls short of full marks because it doesn't say how to *deliberately*
reset the dedup state (e.g., "delete rows from `sent` for a channel to
re-send its backlog" or similar), and it never mentions what a first run —
or a first run for a newly-added channel — actually does. A future maintainer
who wants to intentionally re-send something to one channel has to read the
schema themselves to figure out how.

## 3. What it missed

- **Schema migration for an existing database.** The most important gap: no
  `ALTER TABLE`, no schema-version check, nothing. Verified to crash on an
  existing `items` table from before this change (reproduction under Bugs).
- **First-run / new-channel backfill.** Never named as a decision. Current
  behavior (send everything currently matching a filter) is reasonable but
  silent — a maintainer adding a new channel next month will have no way to
  know in advance that its first digest will include the entire current feed
  window rather than "only things from now on."
- **How to reset dedup state on purpose.** The README tells you not to delete
  the db by accident, but never tells you what to do if you *want* to
  re-deliver something (e.g. a channel's filter changed and people want the
  backlog).
- **Archive staleness on edit.** `INSERT OR IGNORE` means the `items` archive
  keeps the first-seen title/summary forever, even though the point of
  canonicalizing on link was explicitly to tolerate edits. Not wrong for the
  stated purpose of the table, but unremarked.
- **No fallback identity.** The one edge case the docstring itself names
  (article moves to a genuinely new URL) is treated as unsolvable rather than
  mitigated with, say, a secondary check against `raw_id` when available.

## 4. Bugs

**Crash on an existing (pre-fix) database.** This is the one outright break.
Reproduction:

```sh
# 1. Build a db with the ORIGINAL schema/code (simulates the box today)
cd original-notify-digest-checkout
cp config.example.toml config.toml
python3 digest.py --config config.toml --db old.sqlite3

# 2. Deploy the agent's digest.py/store.py over the same directory,
#    pointing at the SAME db file, as would happen on the next cron tick
cp <agent>/digest.py <agent>/store.py .
python3 digest.py --config config.toml --db old.sqlite3
```

Result:

```
sqlite3.OperationalError: table items has no column named norm_link
```

`store.SCHEMA` uses `CREATE TABLE IF NOT EXISTS`, which is a no-op when
`items` already exists under the old schema (no `norm_link` column, no
`UNIQUE` constraint). There is no migration path, so this fix — deployed
exactly as the prompt describes, onto a job that has already been running
from cron — takes down the job on its first invocation after deploy, until
someone notices and either drops the db or hand-migrates it. Given the
prompt's framing ("we run digest.py from cron every 15 minutes"), this is not
a hypothetical: it is the actual deploy path.

Everything else I tested (repeat-run suppression, cross-snapshot dedup with
rotated `utm_campaign`, edited-guid, edited-title-same-link, `--dry-run`
non-consumption of state, genuinely-new-item delivery) worked as designed.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 5.5,
  "failure_mode_reasoning": 7.5,
  "existing_code_respect": 4.5,
  "code_quality": 2,
  "documentation": 3,
  "total": 31.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Sound link-based dedup, well argued, but crashes on any pre-existing database."
}
```
