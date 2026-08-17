# Review — notify-digest, run-2

## 1. Summary

The agent added a `seen` table keyed on a normalised, tracking-param-stripped
link and used it to filter out previously-digested items before they reach
any channel, correctly suppressing repeats across all three feed formats
including edited items and rotated `utm_campaign`/regenerated `guid` cases.
It documented the identity choice and its consequences thoroughly in the
README, but the marking-as-seen happens for *all* channels at once, before
any channel delivery is attempted, which means a single channel outage
silently and permanently drops items for every channel (not just the failing
one) — a real regression against the codebase's own stated "cron will retry"
guarantee that is never discussed anywhere. I would merge this with fixes: the
dedup core is right, but the failure-mode bug needs a real fix (mark per
channel, after successful send) before it goes near a cron box with a flaky
webhook.

## 2. Per-category scoring

### Identity strategy — 9/10

`store.py:54-74` (`normalise_link`) strips `utm_*` query params, lowercases
the host, and drops trailing slashes, then keys on the resulting tuple:

```python
def normalise_link(link):
    parts = urlsplit(link)
    query = tuple(
        kv.split("=", 1)
        for kv in parts.query.split("&")
        if kv and not kv.split("=", 1)[0].startswith("utm_")
    )
    return (
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/"),
        query,
    )
```

I verified this against both fixture snapshots via a scratch copy in `/tmp`:
running snapshot-a then snapshot-b correctly suppressed the blogroll item
whose title changed but link didn't, the wire item whose `guid` was
regenerated (`wire-2026-08-14-0031` → `...-r2`), and the newsroom item whose
`utm_campaign` rotated (`w33` → `w34`) — only the genuinely new "Union
responds to port fee inquiry" item was sent on the second run. This survives
all three feeds' quirks (`blogroll` has no id at all; `generic`'s guid churns
on edit; `newsroom`'s tracking params rotate), and the failure modes are
stated in the README (`README.md`: "An edited item... is not re-sent", "If a
link moves permanently, the item is re-sent once"). Docked half a point for
never falling back to `raw_id` even where it is stable (e.g. `newsroom`'s
`entry_id`) and for not normalising query-param *order*, which the fixtures
don't exercise but a real feed could.

### Ambiguity handling — 3/8

Two of the three named forks are resolved and documented explicitly and
correctly:
- **Edited item as new vs. same** — `store.py:16-21`/README: "Edits to an item
  ... are NOT re-sent. For a 15-minute cron loop that is the right call."
- **First-run backfill** — `store.py:86-99` seeds `seen` from the existing
  `items` archive on connect, and the README states: "If you delete the
  database, everything currently in the feeds will be re-sent."

The third fork — **per-channel vs. global suppression** — is not named
anywhere in the README or code, and is decided *silently and wrong*: it's
global. `digest.py:32-47` computes freshness once per feed, before the
channel loop even starts:

```python
fresh = store.new_items(db, feed_cfg["name"], items)
...
all_items.extend(fresh)
```

and `store.new_items` (`store.py:119-142`) writes to `seen` in the same call
that determines freshness — before any channel has seen the item. Two
channels wanting the same item, one succeeding and one failing to send, both
get the item marked "seen" regardless. This is exactly the "typically global
suppression" case the rubric calls out, so this lands in the 2-3 band; I'm
giving it 3 for the two forks that are handled well.

### Failure-mode reasoning — 2.5/8

The docstring for `new_items` (`store.py:119-125`) argues one specific
tradeoff — atomicity of the SQL transaction against a *crash*:

```python
def new_items(conn, source, items):
    """Return the subset of `items` we have not digested yet.

    An item is "new" when its normalised link is not in `seen`. Everything
    returned is recorded in `seen` in the same transaction, so a crash after
    this call cannot re-send the same items.
    """
```

But this reasoning only covers "crash during recording," not "channel
delivery fails." I reproduced the actual failure mode with a webhook pointed
at a closed port:

```
$ python3 digest.py --config config-fail.toml --db fail.sqlite3
...
channels.DeliveryError: ops: <urlopen error [Errno 61] Connection refused>
exit=1
$ python3 digest.py --config config-fail.toml --db fail.sqlite3
note: feed newsroom has no new items
sent 0 items
```

The item was marked `seen` in `run_once` *before* `channels.send` was ever
called (`digest.py:44` runs before the channel loop at `digest.py:52-61`), so
the failed delivery is never retried — the item is gone forever, silently.
This directly contradicts the pre-existing, unmodified comment in
`channels.py`:

```python
"""...Delivery is best effort: if a channel is down we let the
exception propagate and cron will pick us up again on the next tick.
"""
```

which is now false and nowhere flagged as such. There is no per-channel
marking, no discussion of at-least-once vs. at-most-once, and the ordering
(mark-before-send) is the wrong way round for the guarantee the rest of the
codebase claims to offer. This is a partial-failure window that loses items
outright, landing at the bottom of the 2-3 band.

### Existing-code respect — 4/6

`feeds.py`, `channels.py`, and `render.py` are untouched; `--dry-run` still
works (verified above); the `items` archive is kept as an archive rather than
repurposed. However, the rubric specifically calls out that reusing `items`
is fine "if the unconditional insert is dealt with" — it wasn't.
`digest.py:40` still calls `store.record_items(db, feed_cfg["name"], items)`
with the *full* fetched list every tick, not just the fresh ones, so the
archive keeps growing by a full feed's worth of rows every 15 minutes forever
regardless of dedup. I confirmed this: three dry-runs against an
already-fully-seen snapshot added 18 archive rows (6 items × 3 runs) while
`seen` stayed at 6. That's an unaddressed pre-existing problem the task
description implicitly asked to be dealt with alongside the dedup fix, and
it's now made worse by running every 15 minutes indefinitely. Also costs a
point for the stale `channels.py` comment (see above) that the change makes
actively misleading.

### Code quality — 3.5/4

Clean, readable additions; the schema migration (backfilling `seen` from
`items` on connect, `store.py:90-97`) is handled sensibly and is idempotent
via `INSERT OR IGNORE`. Minor smell: `_encode_key` serialises the normalised
tuple via `repr()` rather than a proper composite key or hash —

```python
def _encode_key(normalised):
    """... a simple repr of the tuple (which contains only strings) is
    enough."""
    return repr(normalised)
```

— which works but is not a format anyone would want to query or index on
purpose; it's acknowledged rather than hidden, which is worth something.

### Documentation — 3.5/4

The README addition (`README.md`, new "## Deduplication" section) is
substantive, not just a docstring: it explains the identity choice, why
`raw_id` was rejected, the two consequences of using the link (edits not
re-sent, permanent link moves re-sent once), where state lives, and how to
reset (delete the db). It does not mention the per-channel/global suppression
decision at all, nor the channel-failure data-loss behavior uncovered above —
both would matter enormously to "the next person to touch it."

**Total: 25.5/40**

## 3. What it missed

- **Per-channel vs. global suppression** was never named as a decision point
  and was resolved the wrong way (global), which is also the direct cause of
  the failure-mode bug below.
- **Mark-before-send vs. mark-after-send** ordering was not considered at
  all — the docstring argues about crash-during-recording but never about
  crash-or-failure-during-delivery, which is the case that actually matters
  for a channel that can raise `DeliveryError`.
- **The unconditional `items` archive insert** flagged by the rubric as
  needing to be "dealt with" was left exactly as-is, so the archive now grows
  unbounded on every 15-minute tick with no expiry, forever, dedup or not.
- **The now-false claim in `channels.py`** ("cron will pick us up again on
  the next tick") was left in place, unmodified, unflagged — a trap for
  whoever debugs why items keep quietly vanishing on channel outages.
- No TTL or eventual pruning of `seen` is mentioned as a non-goal in code,
  only in the README ("Nothing to do about that without a TTL on `seen`,
  which we have not added.") — fine as a call, but there's no size bound
  anywhere, matching the `items` growth problem above.

## 4. Bugs

**Items are lost forever on any channel delivery failure**, not just
deferred to the next tick. Reproduction (also shown above):

```sh
cd /tmp/nd   # scratch copy of the agent's tree
cat > config-fail.toml <<'EOF'
[[feeds]]
name = "newsroom"
format = "newsroom"
url = "file://fixtures/snapshot-a/newsroom.json"

[[channels]]
name = "ops"
type = "webhook"
url = "http://127.0.0.1:1/nonexistent"
title = "Ops"
keywords = []
EOF
python3 digest.py --config config-fail.toml --db fail.sqlite3
# -> DeliveryError, connection refused, exit 1
python3 digest.py --config config-fail.toml --db fail.sqlite3
# -> "note: feed newsroom has no new items" / "sent 0 items"
# the item that was never actually delivered is never offered again
```

Because `store.new_items` (`store.py:119-142`) is called and commits to
`seen` *before* `run_once` even enters the channel loop, this happens
regardless of which channel fails, and regardless of whether other channels
that wanted the same item succeeded. On a real cron box this means: any
webhook hiccup silently and permanently drops whatever items were in flight
at that tick, with no error surfaced beyond a stack trace in the cron log
that nobody is likely to correlate with "we're missing an item."

**Unbounded archive growth** (not a regression, but left unaddressed despite
being squarely in scope): `digest.py:40` archives every fetched item every
tick regardless of freshness, so `items` grows by a full feed's item count
every 15 minutes forever. Confirmed: 3 dry-runs against an already-seen
snapshot added 18 rows to `items` while `seen` stayed flat at 6.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 3,
  "failure_mode_reasoning": 2.5,
  "existing_code_respect": 4,
  "code_quality": 3.5,
  "documentation": 3.5,
  "total": 25.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid link-based dedupe, but marks items seen before delivery, losing them on channel failure."
}
```
