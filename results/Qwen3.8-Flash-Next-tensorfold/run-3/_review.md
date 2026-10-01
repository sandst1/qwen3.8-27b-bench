# Review: notify-digest dedup fix

## 1. Summary

The agent added a per-channel delivery ledger (`delivered` table in `store.py`) keyed on a
query-string-stripped link, gated the existing `items` archive insert so it no longer
duplicates rows forever, added a one-time upgrade seed so switching to the fix doesn't
cause one final resend storm, made `--dry-run` genuinely side-effect-free, and wrote both a
test suite and a long README section explaining every one of these choices. I would merge
this with one follow-up fix: the upgrade-seed step marks an item as "delivered" to *every*
configured channel regardless of whether that channel's filter ever actually matched it,
which silently drops a legitimately-new match after a content edit on a pre-existing link
(reproduced below) — a real, if narrow, bug the agent's own tests don't catch.

## 2. Per-category scoring

### Identity strategy — 10 / 10

`store.identity()` (store.py:58-74) strips scheme/host-case and drops the query string and
fragment, keeping only `scheme://host/path`:

```python
def identity(link):
    """The dedup key for an item: its link with scheme/host lower-cased and
    the query string and fragment stripped.
    ...
    Trade-off to revisit if a new feed ever keys real content in the query
    string (`/view?article=123`-style URLs): this rule would fold such
    distinct articles together. None of the three current providers does.
    """
    parts = urlsplit(link)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))
```

I verified this against all three fixture failure modes directly:

- `newsroom`'s only churn between snapshot-a/b is `utm_campaign=w33` → `w34` on the *same*
  path — suppressed correctly (`sent 2 items`, only the genuinely new story, confirmed below).
- `blogroll` has no id at all (`raw_id: None`) and the post is retitled between snapshots —
  suppressed correctly because the link doesn't change.
- `generic`/`wire` regenerates its `guid` on edit (`wire-2026-08-14-0031` →
  `...-0031-r2`) — suppressed correctly, same reasoning.

Ran it for real:

```
$ python3 digest.py --config config.toml --db digest.sqlite3            # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3            # same tick again
sent 0 items
$ python3 digest.py --config config-b.toml --db digest.sqlite3          # snapshot-b
=== ops ===
* Union responds to port fee inquiry  [newsroom]
=== everything ===
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```

Only the one real new story crossed; the retitled blog post, the edited wire item, and the
utm-rotated repeats of old stories were all suppressed. The chosen failure mode (a feed that
encodes real content in the query string, e.g. `?article=123`) is stated explicitly in the
docstring rather than discovered later. This is exactly the rubric's top-band description.

### Ambiguity handling — 6.5 / 8

All three named forks are explicit, in both code and README:

- **Per-channel vs. global**: `delivered` is keyed `(channel, source, key)` and the README
  states the consequence plainly ("an item matching two filters appears once in *each*
  digest... That is deliberate").
- **Edited item as new vs. same**: resolved as *same*, argued in `identity()`'s docstring.
- **First-run backfill**: resolved via `seed_ledger()`, argued in its docstring and in the
  README's "Upgrading a pre-fix database" section.

That's top-band presentation. But the backfill resolution has a real bug (see **Bugs**
below): `seed_ledger` seeds every archived `(source, link)` as delivered to *every* channel
in the config, not just the channels whose filter actually matched it historically:

```python
# store.py:140-146
conn.executemany(
    "INSERT OR IGNORE INTO delivered (channel, source, key) VALUES (?, ?, ?)",
    [
        (name, r["source"], identity(r["link"]))
        for name in channels
        for r in archived
    ],
)
```

The docstring justifies this as safe ("anything already sitting in `items` had in fact
already been sent to everyone many times over") — but that premise is false. The *original*
`run_once` only ever sent a channel the items that matched its filter
(`original/notify-digest/digest.py:47`, `selected = [i for i in all_items if matches(i, chan_cfg)]`),
so an item that never matched a channel was never sent to it, and seeding it as "delivered"
for that channel anyway is a fabricated delivery record. The fork is named and the general
intent (quiet upgrade) is right; the specific implementation isn't what the docstring claims.
That keeps this out of the top band.

### Failure-mode reasoning — 8 / 8

Marking happens per channel, after a successful send, committed immediately
(digest.py:77-82):

```python
channels.send(chan_cfg, body)
# Record only after a successful send, and commit per channel: a
# failing webhook crashes the run (see channels.py), and the items
# not yet marked for that channel are retried on the next tick.
store.mark_delivered(db, chan_cfg["name"], selected)
```

I reproduced the partial-failure window directly: monkey-patched `channels.send` to raise on
the second of three channels.

```
=== ops ===          (sent fine)
crashed: simulated failure
delivered rows: [('ops', 3)]
```

`ops`, which ran before the failure, is correctly marked; `energy` (the failing channel) and
`everything` (never reached) are not, so their items are retried whole on the next tick. The
guarantee is clearly at-least-once (prefer a possible duplicate over silently losing a batch)
and it's argued, not just implemented — both in the inline comment above and in the README
("Delivery state is recorded *after* a successful send, committed per channel, so a failing
webhook retries its items on the next tick instead of silently swallowing them"). This is
the rubric's top-band description, correctly ordered, with the tradeoff stated.

### Existing-code respect — 6 / 6

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are byte-identical to the
original (verified with `diff`). Only `digest.py`, `store.py`, and `README.md` changed, plus
a new `test_digest.py`. `--dry-run` is preserved and, per the new docstring, deliberately made
*more* pure than before — the original unconditionally called `store.record_items` even
during `--dry-run` (original `digest.py:40`), silently writing archive rows during what was
supposed to be a preview; the agent's version gates that (digest.py:55-56) and documents why.
The rubric's explicit "unconditional insert" concern about reusing the `items` archive is
dealt with directly in `record_items` (store.py:84-91), which now de-dupes against what's
already archived for that source before inserting. The new `delivered` table is added via
`CREATE TABLE IF NOT EXISTS`, so it layers onto an existing database with no migration script
needed — confirmed by running the fixed code straight against a store that only had the old
`items` rows (see Bugs' reproduction, which exercises exactly this path).

### Code quality — 3.5 / 4

Readable, well-commented, parameterized SQL, an `INSERT OR IGNORE` with a composite primary
key for idempotent delivery marks, and genuinely dead code (`store.count_items`, unused
anywhere in the original either) removed rather than left to rot. Docked half a point for the
`seed_ledger` docstring's incorrect safety claim (store.py:131-133, "the ledger is
deliberately not used for anything beyond dedup, which is why that shortcut is safe") — this
is presented as a reasoned guarantee but is demonstrably false (see Bugs). Confident-but-wrong
comments are a quality problem independent of the bug itself, because the next reader will
trust the comment instead of re-deriving the invariant.

### Documentation — 3 / 4

The README's "Keeping digests fresh" section is long, specific, and cites the exact fixture
rows that motivate each rule — it explains the dedupe behaviour and what happens on first run
(fresh install vs. upgrading a pre-fix database) in real depth, well past "docstring-level."
It does not, however, say how to **reset** the ledger — there's no mention anywhere of how to
force a re-send (e.g. `DELETE FROM delivered`, or delete the db) if an operator ever needs one,
which the rubric names explicitly as one of three required topics:

```
$ grep -i "reset\|delete\|clear\|wipe" README.md
(no output)
```

Two of the three required topics are covered thoroughly; the third is absent.

## 3. What it missed

- **How to reset the ledger** isn't documented or tooled (no flag, no SQL snippet, no
  "if you need to force a resend, do X"). An operator who needs to manually resurrect one
  item has no guidance.
- **Unbounded growth**: both `items` and `delivered` grow forever with no retention/pruning
  story; not mentioned as a known limitation.
- **Overlapping cron runs**: nothing changed or discussed about two `digest.py` invocations
  running concurrently (e.g. a slow feed fetch overlapping the next 15-minute tick); SQLite's
  default locking will serialize writers but the agent didn't note this as a consideration
  either way.
- **New channel added later**: not tested or documented, though I checked it behaves sensibly
  by accident of the design — a channel absent from `delivered` naturally receives "the
  current feed window" on its first run, the same as the documented fresh-install case. It
  works, but nobody chose or wrote that down; it's inherited from the bug described below, not
  a separate deliberate decision.

## 4. Bugs

**`seed_ledger` marks items as delivered to channels that never matched them, which can later
suppress a genuinely new match.**

Repro (run against the agent's own code, unmodified, in a scratch copy):

```python
import store, feeds, digest, tomllib
cfg = tomllib.load(open("config.toml", "rb"))
db = store.connect("legacy.sqlite3")

# Simulate a pre-fix legacy database: items already archived, nothing delivered yet.
for feed_cfg in cfg["feeds"]:
    store.record_items(db, feed_cfg["name"], feeds.fetch(feed_cfg))

digest.run_once(cfg, db, dry_run=False)   # first run on the fixed code -> triggers seed_ledger

# "Notes on port fee arithmetic" only ever matched the 'ops' channel's keywords
# (port/levy/fee). 'energy' (tender/grid/offshore) never matched it, under the
# old code or the new one.
row = db.execute(
    "SELECT * FROM delivered WHERE channel='energy' AND source='blogroll' "
    "AND key LIKE '%arithmetic%'"
).fetchall()
print(row)
```

Output:

```
[{'channel': 'energy', 'source': 'blogroll',
  'key': 'https://blog.example/port-fee-arithmetic', 'sent_at': '...'}]
```

`energy` is marked as having already received an item it never matched. Now simulate that
same post being edited (same link, so same identity) to genuinely become relevant to the
energy desk — e.g. a follow-up paragraph tying the levy to the regional power grid:

```python
# same db/session as above
blog_items = feeds.fetch(cfg["feeds"][1])
for it in blog_items:
    if "arithmetic" in it["link"]:
        it["title"] = "Notes on port fee arithmetic, now covering the grid tie-in"
        it["summary"] = "Working through the levy numbers and the offshore grid angle."
# ... patch feeds.fetch to return blog_items for 'blogroll', then:
sent = digest.run_once(cfg, db, dry_run=False)
print(sent)   # -> 0
```

`energy` never receives the now-matching item. Nothing crashes and nothing logs a warning —
it is silently and permanently suppressed for that channel, because `seed_ledger` fabricated
a delivery record for a channel/item pair that was never actually sent. This only triggers on
an upgrade from a pre-fix database followed by a content edit that changes which channel's
keywords match a previously-seen link — narrow, but real, and not covered by
`test_upgrade_seed_silences_the_old_spam`, which only asserts `sent == 0` on the upgrade tick
itself and never re-checks behaviour after a later edit. The fix is straightforward: seed
`(channel, source, key)` only where `matches(item, chan_cfg)` is true for that channel, the
same filter already applied in the live send path.

```json
{
  "identity_strategy": 10,
  "ambiguity_handling": 6.5,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 3.5,
  "documentation": 3,
  "total": 37,
  "would_merge": "yes-with-fixes",
  "one_line": "Excellent per-channel dedup; upgrade-seed over-marks channels it never actually sent."
}
```
