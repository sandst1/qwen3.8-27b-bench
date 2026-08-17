# Review — claude-opus-4.6-max, run-3

## 1. Summary

The agent added a `sent(channel, link_key)` table to `store.py`, normalized each
item's `link` (stripping query string/fragment) as the dedupe identity, wired
`unsent_for_channel`/`mark_sent` into `digest.py`'s existing per-channel loop,
and hardened `record_items` with `INSERT OR IGNORE` behind a new
`UNIQUE(source, link)` index — a small, surgical diff (2 files touched) that I
verified correctly suppresses repeat sends across a same-snapshot re-run, a
UTM-rotated newsroom link, and per-channel independence. However, the new
unique index on `items(source, link)` is created unconditionally on
`connect()`, and on any database that has already accumulated duplicate
`(source, link)` rows from months of the old unconditional-insert
behavior — i.e., exactly the production database this fix is meant to be
deployed onto — `store.connect()` raises `sqlite3.IntegrityError` and the cron
job never runs again. I would merge this **with fixes**: the identity and
per-channel logic are sound and tested, but the migration path must be fixed
before it touches the real `digest.sqlite3`, and the README needs to actually
say what changed.

## 2. Per-category scoring

### Identity strategy — 8/10

The agent settled on the item's `link`, normalized by stripping query and
fragment, as the identity key — deliberately *not* `raw_id`, which it
correctly diagnoses as unreliable per-feed:

```python
# store.py:7-27
De-duplication strategy
-----------------------
Each feed item's *link* is the most stable identifier across all three
feed formats.  ``raw_id`` is unreliable: the blogroll provider omits it
entirely, and the wire provider regenerates the GUID on every edit.
The newsroom provider's ``entry_id`` is stable but its feed URLs carry
UTM campaign parameters that rotate weekly.

We therefore derive a *link_key* — the link stripped of query string and
fragment — and use it as the identity of an item in the ``sent`` table.
```

```python
# store.py:57-65
def _normalize_link(link):
    p = urlparse(link)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
```

I verified this against the fixtures directly (fresh db, `snapshot-a` then
`snapshot-b`):

```
$ python3 digest.py --config config.toml --db test.sqlite3      # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db test.sqlite3      # same snapshot again
sent 0 items
$ sed -i '' s/snapshot-a/snapshot-b/ config.toml
$ python3 digest.py --config config.toml --db test.sqlite3
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```

The newsroom article whose URL rotated `utm_campaign=w33` → `w34`
(`entry_id 84121`, same path) was correctly *not* resent; only the genuinely
new article went out. That's the strategy holding up against the UTM-rotation
case from `fixtures/snapshot-a/newsroom.json` vs `snapshot-b/newsroom.json`,
and the blogroll's missing `raw_id` is moot because identity never depends on
it.

What keeps this from a 9–10: the docstring states the *rationale* for picking
link-over-raw_id, but doesn't name the failure mode of the chosen
strategy itself — e.g. two distinct items that happen to share a URL path but
are disambiguated only by query string (not present in the fixtures, but a
real risk of "strip everything after `?`") is never flagged. The reasoning
about the three *feeds'* raw_id quirks is thorough; the reasoning about the
*link-normalization scheme's own* edge cases is absent.

### Ambiguity handling — 6/8

Three forks exist per the rubric: per-channel vs. global suppression, edited
item as new vs. same, first-run backfill.

- **Per-channel vs. global**: resolved correctly and named in the schema
  comment — `sent` is keyed `(channel, link_key)`, not just `link_key`:

  ```python
  # store.py:48-53
  CREATE TABLE IF NOT EXISTS sent (
      channel     TEXT NOT NULL,
      link_key    TEXT NOT NULL,
      sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
      PRIMARY KEY (channel, link_key)
  );
  ```
  I verified this is genuinely per-channel: in the crash test below, `ops`
  got marked sent while a second, failing channel did not — each channel has
  its own suppression state.

- **Edited item as new vs. same**: resolved (edits are treated as duplicates,
  since only the link is compared) and *explicitly named* in the docstring:

  ```python
  # store.py:17-21
  This correctly collapses:
  * the same newsroom article across UTM rotations,
  * wire edits that only change the GUID, and
  * blogroll updates that only change the title or excerpt.
  ```
  I confirmed this in `fixtures/snapshot-b`: the wire item whose `guid`
  changed from `wire-2026-08-14-0031` to `...-r2` and whose description grew
  an extra sentence, and the blogroll post retitled "(updated)" with an
  expanded excerpt, were both silently suppressed as duplicates on the second
  run — nobody is notified of the correction. That's a real, consequential
  editorial choice (an operator reading "Port fee inquiry opened" will never
  learn the regulator's follow-up comment was added), and while it's named in
  the docstring, it isn't argued or surfaced in the README/CLI output for a
  human operator to reconsider.

- **First-run backfill**: not addressed anywhere. I ran against a brand-new
  database directly on `snapshot-b` and it dumped all 14 matching items to
  every channel in one digest with no comment on this being the intended
  first-run behavior:

  ```
  $ python3 digest.py --config config.toml --db test2.sqlite3
  ...
  sent 14 items
  ```
  Reasonable default, but silent — never named as a decision.

Two of three forks are explicitly named and resolved correctly; the third is
silent-but-reasonable. That's squarely the 4–6 band, at the top of it.

### Failure-mode reasoning — 5/8

`channels.py`'s existing docstring already establishes the intended
guarantee: *"Delivery is best effort: if a channel is down we let the
exception propagate and cron will pick us up again on the next tick."*
(at-least-once, per invocation). The agent's marking is per-channel and
ordered correctly relative to that existing contract — `mark_sent` is called
only after `channels.send` returns without raising, immediately followed by
its own commit:

```python
# digest.py:56-57
channels.send(chan_cfg, body)
store.mark_sent(db, chan_cfg["name"], selected)
```

I reproduced a partial failure directly: a config with a working `stdout`
channel (`ops`) followed by a `webhook` channel pointed at a closed port
(`broken`). `ops` sent, got marked, and `broken` raised, killing the process:

```
$ python3 digest.py --config config_crash.toml --db crash.sqlite3
=== ops ===
... (2 items) ...
channels.DeliveryError: broken: <urlopen error [Errno 61] Connection refused>
exit: 1
$ sqlite3 crash.sqlite3 "select * from sent;"
ops|https://newsroom.example/2026/08/port-fees|...
ops|https://newsroom.example/2026/08/offshore-tender|...
$ python3 digest.py --config config_crash.toml --db crash.sqlite3   # retry
channels.DeliveryError: broken: ...   # same failure again, ops NOT resent
```

This is the *correct* outcome for the stated guarantee: `ops` is not
double-notified on retry, `broken` will get its items once it recovers, no
batch is silently dropped. That's good, working per-channel at-least-once
behavior — but it is a side effect of ordinary statement ordering, not an
argued decision. Nothing in the docstring, README, or a comment says
"marking happens after send so a downstream channel failure can't cause a
successful channel to be re-notified" or discusses what happens if the
process is killed *between* `send()` returning and `mark_sent()`'s commit
(a narrow but real at-most-once/at-least-once boundary). Per-channel marking
and sane ordering with zero discussion lands this at 5/8, not higher.

### Existing-code respect — 4/6

The diff is minimal and disciplined: only `digest.py` (3 lines changed) and
`store.py` were touched; `feeds.py`, `channels.py`, `render.py`,
`config.example.toml` are byte-identical to the original. `--dry-run` was
kept working correctly — I confirmed `mark_sent` is only called in the
non-dry-run branch (`digest.py:52-57`), so repeated `--dry-run` invocations
are still side-effect-free and idempotent, exactly matching original
semantics.

The rubric explicitly allows reusing the `items` archive "if the
unconditional insert is dealt with" — the agent did address that
(`INSERT OR IGNORE` + `UNIQUE(source, link)`), but the *manner* in which it
dealt with it breaks existing deployments (see Bugs below): the new unique
index is created unconditionally in `SCHEMA` and run every time `connect()`
is called, with no migration step for a database that already has duplicate
`(source, link)` rows — which is guaranteed to exist on the very production
box this cron job has supposedly been running on. That's a direct
violation of the "existing-code respect" concern the rubric calls out by
name, which caps this below a clean score.

### Code quality — 2.5/4

The added code itself is clean, small, and consistent with the existing
style (plain functions, `sqlite3.Row`, executemany, explicit SQL). No dead
code, and the `raw_id` column is correctly left alone for archival purposes
(the module docstring says the archive is "everything we have ever fetched,"
consistent with original intent). SQL is sensible (`INSERT OR IGNORE`,
composite primary key on `sent`). The one significant defect — schema
migration not handled for an existing database, causing a hard crash on
upgrade (see Bugs) — is exactly the kind of "schema migration handled for
an existing database" item the rubric names for this category, and it's not
handled at all.

### Documentation — 2/4

The README is **byte-for-byte unchanged** from the original:

```
$ diff original/notify-digest/README.md .../run-3/README.md
(no output)
```

All of the explanation lives in the `store.py` module docstring (quoted
above under Identity strategy). Per the rubric, *"A docstring alone caps
this at 2"* — that ceiling applies regardless of how good the docstring's
content is. On top of that, the docstring doesn't mention how to reset the
dedupe state (e.g., "delete rows from `sent`, or drop the whole db to
resend everything") or what a first run looks like (a full backfill digest).
Given the docstring is genuinely well-written but the cap applies and two
of the three items the rubric names (reset instructions, first-run
behavior) are missing even from the docstring, this sits at 2/4, not the
top of the capped range.

## 3. What it missed

- **No note that anything changed at all in README.md.** Someone reading the
  README for this repo today would have no idea a dedupe fix landed, what
  identity strategy was chosen, or how to reset it. All of that context is
  buried in `store.py`'s module docstring only.
- **No reset/backfill instructions.** If someone needs to re-send everything
  (e.g. after a bad digest went out under an old bug), there's no documented
  way to do it (e.g. `DELETE FROM sent WHERE channel = ?`).
- **No migration for existing databases.** See Bugs — this is the most
  serious omission, since it's not a hypothetical: it's the described
  deployment (`cron every 15 minutes`) that has necessarily been
  accumulating duplicate `(source, link)` rows.
- **Editorial re-sends are silently suppressed.** An edited wire/blogroll
  item (new content, same link) never reaches subscribers again. This may be
  the right call, but it's a real product decision (people might want to see
  substantive corrections) that isn't surfaced anywhere a human would see it
  before merging.
- **No test added.** The fixtures are clearly built for exactly this kind of
  before/after check, but no automated test exercises `snapshot-a` →
  `snapshot-b` or the crash/retry path; I had to verify all of this by hand.

## 4. Bugs

**Schema migration crash on any pre-existing database with duplicate
`(source, link)` rows.**

`store.py`'s `SCHEMA` unconditionally creates a unique index over
`(source, link)` every time `connect()` runs:

```python
# store.py:45
CREATE UNIQUE INDEX IF NOT EXISTS uq_items_source_link ON items(source, link);
```

The original code inserted a fresh row into `items` on *every* fetch, with no
uniqueness constraint — meaning a cron job that "keeps getting the same items
over and over," running every 15 minutes for any nontrivial period, has
necessarily accumulated many duplicate `(source, link)` rows in its `items`
table already. Reproduction:

```python
import sqlite3
conn = sqlite3.connect("legacy.sqlite3")
conn.execute("""CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, raw_id TEXT, title TEXT NOT NULL, link TEXT NOT NULL,
    summary TEXT, published TEXT,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')))""")
for _ in range(5):
    conn.execute("INSERT INTO items (source, raw_id, title, link) "
                  "VALUES ('wire','g1','t','https://wire.example/i/0031')")
conn.commit(); conn.close()
```

```
$ python3 -c "import store; store.connect('legacy.sqlite3')"
FAILED: UNIQUE constraint failed: items.source, items.link
```

`store.connect()` raises `sqlite3.IntegrityError` and never returns a
connection. `digest.py`'s `main()` doesn't catch this — it propagates
straight out of `main()`, so the process exits non-zero and cron logs an
unhandled traceback every 15 minutes forever, with no digests sent to
anyone, until an operator manually intervenes (e.g. by deleting duplicate
rows or the whole database). Given the task is specifically about a script
that already "runs from cron every 15 minutes" and already has duplicates,
this is not an edge case — it is the default state of the database the fix
will be deployed onto.

```json
{
  "identity_strategy": 8,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 5,
  "existing_code_respect": 4,
  "code_quality": 2.5,
  "documentation": 2,
  "total": 27.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Sound link-based dedupe, but crashes on any pre-existing db with duplicates."
}
```
