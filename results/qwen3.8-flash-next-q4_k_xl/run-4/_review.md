# Review: notify-digest dedup fix (qwen3.8-flash-next-q4_k_xl, run-4)

## 1. Summary

The agent added a per-channel delivery ledger (`delivered(channel, dedup_key)`), derived
identity from a tracking-param-stripped canonical link instead of the per-feed `raw_id`,
fixed the archive table's unconditional `INSERT` with `INSERT OR IGNORE` on a unique key,
migrated existing databases in place, and wrote a 7-test regression suite plus a
substantial README section explaining all of this. I verified the core claims by running
`digest.py` against both fixture snapshots by hand (not just the agent's own tests): a
repeat run of the same snapshot sends 0 items, and snapshot-b — which edits the wire
item's `guid`, the blogroll title, and every newsroom `utm_campaign` — correctly sends
only the one genuinely new "union" story. I would merge this with minor fixes: the
identity/ledger design is sound and well-argued, but a query-parameter-ordering bug in
`canonical_link` and an un-mentioned multi-channel failure cascade should be addressed or
at least flagged before shipping.

## 2. Per-category scoring

### Identity strategy — 9 / 10

The agent rejected both `raw_id` and the raw link as universal keys and instead built the
identity from the link with tracking parameters and fragment stripped, scoped by feed
source. This is stated with its failure modes for each of the three feeds, not just
asserted:

```python
# feeds.py
TRACKING_PARAM_PREFIXES = ("utm_", "mc_", "pk_")

def canonical_link(link):
    """Strip tracking params and the fragment so e.g. the same newsroom story
    with utm_campaign=w33 and w34 collapses to one URL."""
    parts = urlsplit(link)
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(TRACKING_PARAM_PREFIXES)
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(kept), "")
    )

def item_key(item):
    """Stable identity for an item. See the module docstring for why this is
    the canonical link and not raw_id."""
    return f"{item['source']}|{canonical_link(item['link'])}"
```

I verified this against the real fixtures, not just the agent's assertions. Running the
CLI on `snapshot-a` then `snapshot-b`:

```
$ python3 digest.py --config config.toml --db test.sqlite3   # snapshot-a
sent 9 items
$ python3 digest.py --config config.toml --db test.sqlite3   # snapshot-a again
sent 0 items
$ python3 digest.py --config config.toml --db test.sqlite3   # snapshot-b
--- ops ---
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```

Only the new "union" story goes out, despite `wire`'s guid changing
(`wire-2026-08-14-0031` → `...-0031-r2`), `blogroll`'s title changing
("...arithmetic" → "...arithmetic (updated)"), and every `newsroom` URL's
`utm_campaign` rotating (`w33` → `w34`). All three lies in the fixtures are handled.

Docked one point for a real edge case the strategy doesn't cover: `canonical_link`
preserves the *order* of the surviving query parameters. Two crawls of the same URL that
differ only in the order of two non-tracking parameters produce two different keys:

```
$ python3 -c "
import feeds
print(feeds.canonical_link('https://x.example/a?id=5&lang=en&utm_campaign=w33'))
print(feeds.canonical_link('https://x.example/a?lang=en&id=5&utm_campaign=w34'))
"
https://x.example/a?id=5&lang=en
https://x.example/a?lang=en&id=5
```

Not exercised by the given fixtures (which have zero or one surviving param), and not
what causes the bug the user reported, but it is a genuine gap in a strategy the header
comment presents as fully general ("the one signal that stays put across every
provider").

### Ambiguity handling — 7 / 8

All three named forks in the rubric are addressed:

**Per-channel vs. global suppression** — named and resolved explicitly, in code and prose:

```python
# store.py
"""
  * `delivered` is the ledger that actually stops duplicate digests: one row
    per (channel, dedup_key) we have successfully sent. Dedup is deliberately
    per channel — a story that only matches `ops` must still be eligible for
    `energy` the first time it appears, and a channel that was down and
    retried must not lose its items.
"""
```
Verified: `test_dedup_is_per_channel` in `test_dedup.py:94` and my own run show `ops` and
`energy` accumulate disjoint delivered sets.

**Edited item as new vs. same** — this is the identity discussion above, stated with a
comparison table in the README (lines 40–46) covering all three feeds' edit behavior.

**First-run backfill** — named, but only from the angle of *migrating an existing
deployment*, not a genuinely fresh install:

```markdown
Old databases are migrated in place on startup (`store._migrate`): the pre-dedup
`items` table gains a `dedup_key` column without losing rows, and the `delivered`
ledger starts empty, so the first run after deploying may resend once.
```

Behaviorally this is the same case as a brand-new install (empty `delivered` table
either way), and the consequence — the entire current batch across all matching
channels goes out once — is correctly described. But the agent never poses the
question "should a fresh deploy blast the whole current feed contents, however large the
backlog, to every channel on run one?" as a considered choice; it's presented as an
unavoidable side effect of migration rather than an explicit design decision for a truly
new install. That framing gap is why this isn't full marks.

### Failure-mode reasoning — 7 / 8

Per-channel marking, correct write-after-send ordering, and the at-least-once tradeoff
are all explicit:

```python
# digest.py
channels.send(chan_cfg, body)
# Record only after a successful send: if delivery raised, nothing is
# written for this channel and the items are retried on the next tick.
store.record_deliveries(db, chan_cfg["name"], [i["dedup_key"] for i in selected])
```

I reproduced the "channel two fails after channel one succeeded" scenario directly
(three-channel config, middle channel is an unreachable webhook):

```
$ python3 digest.py --config config_fail3.toml --db fail3.sqlite3
=== ops ===
...
channels.DeliveryError: broken: <urlopen error [Errno 61] Connection refused>
$ sqlite3 fail3.sqlite3 "select * from delivered"
ops|newsroom|https://newsroom.example/2026/08/port-fees|...
```

`ops`'s delivery is correctly committed and will not be resent; `broken` is correctly not
marked and will retry next tick. That much matches the stated guarantee.

What is *not* discussed anywhere: the uncaught `DeliveryError` aborts `run_once` entirely,
so the third channel (`after`, alphabetically/positionally past `broken`) is never even
attempted on this tick — confirmed above, `after` has no row in `delivered` and never
printed. If `broken` stays broken indefinitely (a bad URL that's never fixed), every
channel configured after it in `cfg["channels"]` is silently starved forever, not just
delayed. This is inherited from the pre-existing `channels.py` docstring's assumption
("let the exception propagate and cron will pick us up again") which predates this task
and is outside its scope, so I have not scored it down as harshly as a self-inflicted
bug — but the fix's own commentary about "correct ordering for the stated guarantee"
stops short of noticing that its guarantee is per-*pair*-of-adjacent-failing-channel, not
per-channel-independent. Half point off for that omission, not for the underlying
architecture, which the agent didn't touch and wasn't asked to.

### Existing-code respect — 6 / 6

`channels.py`, `render.py`, and `config.example.toml` are untouched (`diff` is empty for
all three). `feeds.py`'s per-format branches are minimally reshaped to attach a shared
`dedup_key` field at the end rather than duplicating that line three times — not a
rewrite. The rubric explicitly calls out the archive table's unconditional insert as
something that must be "dealt with" if the `items` table is kept; it is:

```python
# store.py
"INSERT OR IGNORE INTO items"
" (source, raw_id, dedup_key, title, link, summary, published)"
" VALUES (?, ?, ?, ?, ?, ?, ?)",
```

backed by `CREATE UNIQUE INDEX IF NOT EXISTS idx_items_dedup ON items(dedup_key)`. Old
databases are migrated rather than assumed fresh (`store._migrate`, tested by
`MigrationTest` and independently reproduced by me against a hand-built pre-dedup
schema — the column is added, the one existing row survives). `--dry-run` still prints
without recording:

```
$ python3 digest.py --config config.toml --db dryrun.sqlite3 --dry-run   # x2
--- would send to ops --- (identical both times, 4 items, sent 0 items)
```

No scope creep (no plugin system, scheduler, or web UI).

### Code quality — 3.5 / 4

Clean, well-commented, no dead code, no vestigial columns — `raw_id` is kept but still
meaningfully populated from the provider and used for the archive, not orphaned. Sensible
SQL: composite primary key on `delivered(channel, dedup_key)`, a partial migration that
only runs `ALTER TABLE` when the column is actually missing. Docked half a point for the
query-parameter-ordering gap in `canonical_link` (see Identity strategy) going both
unnoticed and untested — it's a real defect in code that is otherwise careful about edge
cases (it does normalize scheme/host case and strip the fragment).

### Documentation — 3 / 4

Far more than a docstring: a dedicated README section ("How duplicate suppression works")
with a comparison table per feed, the per-channel rationale, the archive's separate
purpose, the migration/first-run consequence, and how to run the test suite. It covers
"what happens on first run" (via the migration framing noted above) and "the dedupe
behaviour" thoroughly. It does **not** cover "how to reset it" — there's no note on what
to delete or truncate (`delivered` rows for a channel, or the whole sqlite file) to force
a resend, which the rubric asks for explicitly. I grepped the whole README and found
nothing on `reset`/`delete`/`drop table`/`clear`.

## 3. What it missed

- **How to reset dedup state.** No operator instructions for "I want channel X to resend
  everything" or "wipe the ledger after a bad deploy." A one-liner (`DELETE FROM delivered
  WHERE channel='ops'` or similar) would have closed this.
- **Truly-fresh-install framing of the backfill decision.** The agent reasons about backfill
  only from the "migrating an existing deployment" angle; it never poses the question for a
  brand-new install with no prior `items` rows at all, even though the resulting behavior
  (send the whole current batch once) is identical either way.
- **Cross-channel failure cascade.** Nothing in the code or docs acknowledges that one
  permanently-broken channel silently prevents every channel configured after it from
  ever being attempted, not just delayed. See Bugs below for a reproduction.
- **Query-parameter ordering in `canonical_link`.** Presented as fully general ("the one
  signal that stays put"), but order-sensitive; not tested against the possibility.
- No consideration of items whose canonical link is empty/malformed (e.g., a relative
  URL) — `urlsplit`/`urlunsplit` will not raise, but the resulting key's usefulness for a
  malformed link isn't discussed. Low-probability given the fixtures, but unaddressed.

## 4. Bugs

**Bug 1 — query-parameter order changes the identity of an otherwise-identical URL.**
```
$ python3 -c "
import feeds
print(feeds.canonical_link('https://x.example/a?id=5&lang=en&utm_campaign=w33'))
print(feeds.canonical_link('https://x.example/a?lang=en&id=5&utm_campaign=w34'))
"
https://x.example/a?id=5&lang=en
https://x.example/a?lang=en&id=5
```
These are the same page and the same story by every plausible reading, but they produce
different `dedup_key`s and would be delivered twice. Not present in the given fixtures
(their only surviving-after-stripping cases have at most one param), but it is a live
gap in a function whose docstring claims full generality.

**Bug 2 (latent, inherited, undocumented) — a permanently broken channel starves every
channel after it in the config, forever, not just this tick.**
```
$ python3 digest.py --config config_fail3.toml --db fail3.sqlite3
=== ops ===
...
channels.DeliveryError: broken: <urlopen error [Errno 61] Connection refused>
$ sqlite3 fail3.sqlite3 "select channel from delivered"
ops
```
`after` (configured after `broken`) never even gets its `matches`/`select_for_channel`
pass run this tick, and will not until `broken` either succeeds or is removed from the
config — since the uncaught exception aborts `run_once` before the loop reaches `after`.
This predates the agent's change (the original loop had the same lack of isolation) and
was not part of the reported symptom, so I have not scored it as a regression, but the
fix's own commentary about ordering guarantees stops just short of surfacing it.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 7,
  "failure_mode_reasoning": 7,
  "existing_code_respect": 6,
  "code_quality": 3.5,
  "documentation": 3,
  "total": 35.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Sound per-channel ledger + canonical-link identity; missing reset docs, param-order bug."
}
```
