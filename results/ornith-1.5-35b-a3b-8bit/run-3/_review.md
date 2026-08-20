# Review — notify-digest, ornith-1.5-8bit run-3

## 1. Summary

The agent added a real persistence-backed dedupe: a `feeds.identity()` function keyed on `(source, normalized_link)` that strips tracking query params, a `store.seen_keys()`/`record_items()` pair with a unique index, and moved the record-write to after send so failed deliveries retry. It correctly handles the three fixture-specific traps (rotating `utm_campaign`, guid-less blogroll, regenerated wire `guid`) and documents the identity choice in both code and README. However, it introduces a real crash-on-migration bug for any pre-existing database and a duplicate-delivery bug on partial multi-channel failure, and it silently chose global (not per-channel) suppression. I would merge this with fixes — the core idea is sound but it isn't safe to drop onto an existing deployment as-is.

## 2. Per-category scoring

### Identity strategy — 7.5/10

`feeds.py` normalizes links and drops tracking params, keying identity on `(source, normalized_link)`:

```python
# feeds.py
def normalize_link(link):
    scheme, netloc, path, query, _fragment = urlsplit(link)
    keep = [
        part
        for part in query.split("&")
        if part.split("=", 1)[0].lower() not in _TRACKING_PARAMS
    ]
    return urlunsplit((scheme, netloc, path, "&".join(keep), ""))

def identity(item):
    return (item["source"], normalize_link(item["link"]))
```

Verified against both fixture snapshots: re-running `snapshot-a` twice sends nothing the second time, and running `snapshot-b` after `snapshot-a` sends only the genuinely-new "Union responds to port fee inquiry" item — the newsroom items whose `utm_campaign` rotated from `w33`→`w34` are correctly *not* resent, and the wire item whose `guid` was regenerated (`wire-2026-08-14-0031` → `-r2`) is also correctly suppressed since its link didn't change. This is a real, tested strategy that survives all three of the stated failure modes, and the docstring names them explicitly:

```python
"""Stable identity for an item, used to decide whether we've seen it.
...
  - newsroom: entry_id is stable, but the URL carries volatile utm params
    (stripped by normalize_link).
  - blogroll: has no server-side id at all; the permalink is stable.
  - wire: guid is regenerated whenever the item is edited, but the link is
    not, so a rewritten wire story must still be recognised as the same
    item.
"""
```

Docked from the top band because the tracking-param stripper is a fixed allowlist (`_TRACKING_PARAMS`) rather than a documented, extensible policy — any future feed that appends a new analytics param (or reorders query params, which `urlsplit`/rejoin does not canonicalize by sorting) silently defeats dedup again, and nothing flags that risk.

### Ambiguity handling — 4/8

Of the three canonical forks:

- **Edited item as new vs. same**: resolved (silently but correctly, and consistently) — link-based identity means an edited wire/blogroll item is treated as the same item, never re-sent. This is defensible but has a real cost (readers never see substantive corrections, e.g. wire's "Adds operator comment." update in snapshot-b is dropped entirely) and it is not surfaced as a tradeoff anywhere, only implied by the docstring's phrasing.
- **First-run backfill**: not addressed at all. On a brand-new db, `seen_keys()` returns empty, so the first run dumps the entire fetched backlog as "new." README doesn't mention this.
- **Per-channel vs. global suppression**: decided silently, and it's the global choice. `store.py`'s schema has no channel column:

```python
def seen_keys(conn):
    return {
        (row["source"], row["key"])
        for row in conn.execute("SELECT source, key FROM items").fetchall()
    }
```

  Confirmed by inspection of the sqlite rows after a run — items are recorded once, source+key only, no channel. Practically: if a channel's keyword filter is broadened/added later, or if a channel's send fails on one run and the item happens to get marked seen anyway (see failure-mode bug below crossed with this), that channel can permanently miss an item another channel already consumed. Nothing in code or README names this as a decision made.

Only one of three forks is both decided well and stated; the other two are silent, and one of the silent ones (global suppression) has an actual failure mode. That places this in the low half of the 4–6 band.

### Failure-mode reasoning — 3/8

The write-after-send ordering is right in isolation and stated:

```python
# digest.py
channels.send(chan_cfg, body)
sent += len(selected)
...
# Record after sending, so a delivery that failed is retried next run
# instead of being dropped. A dry run never mutates the store.
if new_items and not dry_run:
    store.record_items(db, new_items)
```

But marking is **per-run**, not **per-channel**, despite there being multiple channels in the loop above it. Reproduced: with two channels both matching the same item, one `stdout` (succeeds) and one `webhook` pointed at a closed port (raises `DeliveryError`), `channels.send` for the second channel raises, the exception propagates out of `run_once` uncaught, and the process exits with a traceback before `store.record_items` ever runs:

```
$ python3 digest.py --config config-crash.toml --db crash.sqlite3
=== ops ===
Ops digest — 1 item(s)
* Regulator opens inquiry into port fees  [newsroom] ...
Traceback (most recent call last):
  ...
channels.DeliveryError: broken: <urlopen error [Errno 61] Connection refused>
exit: 1
```

```
$ python3 -c "import sqlite3; print(list(sqlite3.connect('crash.sqlite3').execute('select source,key from items')))"
[]
```

Re-running with just the `ops` channel against the same db then delivers the *same* item again:

```
$ python3 digest.py --config config-crash2.toml --db crash.sqlite3
=== ops ===
Ops digest — 1 item(s)
* Regulator opens inquiry into port fees  [newsroom] ...
sent 1 items
```

This is precisely the bug the prompt describes ("people keep getting the same items over and over"), now scoped to the case of any channel failure downstream of a successful one, on every cron tick until the last channel in the list also succeeds. `channels.py`'s own docstring says delivery is "best effort ... we let the exception propagate," but `digest.py` never catches it per-channel or records per-channel state, so this isn't a per-channel-marking design, it's an all-or-nothing one with a real duplicate-on-partial-failure window — squarely the 2–3 band, and I'm putting it near the bottom of that band because the failure mode is not merely theoretical, it's the same symptom class as the bug being fixed.

### Existing-code respect — 3/6

Positives: didn't rewrite `feeds.py`/`digest.py` wholesale, kept `--dry-run` working (verified: dry-run against a fresh db leaves 0 rows), kept the config/channel/render modules untouched, and reused the `items` table concept rather than inventing a parallel structure.

Negative, and significant: the schema change is not migration-safe for an "existing database," which the rubric explicitly calls out. `store.py`'s `connect()` runs:

```python
CREATE TABLE IF NOT EXISTS items (...)   -- no-op if table exists
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_seen ON items(source, key);
```

Against a database built with the *original* schema (which has `raw_id`, no `key` column), `CREATE TABLE IF NOT EXISTS` is a no-op (table already exists) and the `CREATE UNIQUE INDEX ... (source, key)` then fails outright:

```
$ python3 digest.py --config config-a.toml --db /tmp/old.sqlite3
...
sqlite3.OperationalError: no such column: key
```

This is a hard crash on the first cron run against any real, pre-existing production db — which is exactly the situation "we run this from cron every 15 minutes" implies exists. The README asserts the opposite of what the code does ("a fresh db is fine, but an existing one needs dropping or migrating") without actually providing a migration path or a guard — it's an unbacked claim, not a mitigation.

### Code quality — 3/4

Clean, readable diff; no dead code; the `raw_id`→`key` schema change is deliberate and well-named; SQL is simple and uses `INSERT OR IGNORE` sensibly. Docked half a point for the vestigial-comment risk noted above (schema claims to be migration-friendly and isn't) and the missing per-channel-failure handling being absent rather than explicitly deferred in a comment.

### Documentation — 4/4

The README's new "Deduplication" section is substantive prose, not a docstring stub — it explains the identity choice, why no single field works, when items are recorded relative to sending, and states (even if the code doesn't back it up) that existing dbs need migration:

```markdown
## Deduplication

The cron job runs every 15 minutes, so each run must only deliver items it
hasn't delivered before. ...

Items are recorded only *after* a successful send, so a failed delivery is
retried on the next tick rather than lost. The db schema adds a `key` column and
a unique index on `(source, key)`; a fresh db is fine, but an existing one needs
dropping or migrating to pick up the new column.
```

This meets the bar for full documentation credit on its own terms (explains dedupe behaviour, first-run/reset framing, what happens on migration) even though the migration claim turns out to be false in practice — that's scored as a bug/existing-code-respect failure, not a documentation failure, since the doc's job (explaining intent) is done.

## 3. What it missed

- **First-run backfill** is not discussed anywhere: a fresh deploy immediately mass-delivers the entire current feed backlog on its first tick. May be fine, may not — never named as a choice.
- **Per-channel vs. global suppression** is decided (globally) with no code comment or README line acknowledging it, even though the rubric singles this out as one of the three canonical forks.
- **Edited-item semantics** (same vs. new) are consistently link-keyed but the consequence — corrections/updates to already-sent stories are silently dropped forever — is never stated as a tradeoff, only implied.
- No handling for partial multi-channel failure; the agent's own `channels.py` docstring says exceptions propagate "best effort," but `digest.py` doesn't design around that at all when deciding what to persist.
- No migration script or version check for pre-existing databases, despite the README claiming migration is necessary — it asserts a problem exists without solving it.

## 4. Bugs

**Bug 1 — crash on any pre-existing (pre-fix) database.**
```
$ python3 digest.py --config config-a.toml --db /tmp/old.sqlite3
sqlite3.OperationalError: no such column: key
```
Reproduced by creating a db with the original `raw_id`-based schema (as any real deployment would already have) and running the new code against it. `CREATE TABLE IF NOT EXISTS` silently no-ops, then `CREATE UNIQUE INDEX ... (source, key)` fails because `key` doesn't exist on the old table. The cron job stops working entirely until someone manually drops or migrates the db — for a fix whose whole point is to be dropped into an existing cron job, this is a first-run showstopper.

**Bug 2 — duplicate delivery on partial channel failure.**
```
$ python3 digest.py --config config-crash.toml --db crash.sqlite3   # 2 channels, 2nd fails
=== ops ===
* Regulator opens inquiry into port fees ...
Traceback ...
channels.DeliveryError: broken: <urlopen error [Errno 61] Connection refused>

$ python3 digest.py --config config-crash2.toml --db crash.sqlite3  # ops-only re-run
=== ops ===
* Regulator opens inquiry into port fees ...   # same item, sent again
sent 1 items
```
Because `store.record_items` is called once, after the whole channel loop, an unhandled exception from any channel (including the last one in the list) means nothing gets recorded even though earlier channels already delivered successfully — reproducing the exact "same items over and over" symptom the task asked to fix, just narrowed to the multi-channel-with-one-failing-channel case instead of every run.

```json
{
  "identity_strategy": 7.5,
  "ambiguity_handling": 4,
  "failure_mode_reasoning": 3,
  "existing_code_respect": 3,
  "code_quality": 3,
  "documentation": 4,
  "total": 24.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid link-based identity, verified working, but crashes on existing DBs and can duplicate on partial channel failure."
}
```
