# Review: notify-digest dedupe fix

## 1. Summary

The agent added a `sent_items` table keyed on `(channel, fingerprint)` and gated
sending on it, with `fingerprint = raw_id or link` — a minimal, surgical
two-file diff (`digest.py`, `store.py`) that leaves `feeds.py`, `channels.py`,
and `--dry-run` untouched. It correctly stops re-sending identical items on a
straight cron re-fire, but it silently reuses `raw_id` as the primary key even
for the one feed format (`generic`/`wire`) whose own docstring warns that the
`guid` is regenerated on edit, so that exact case still duplicates, and it
left the pre-existing unconditional `items` insert (explicitly called out in
the rubric) completely unaddressed, so the archive table now grows even
faster under the new per-run-still-recorded model. I would merge this
with fixes: the common-case duplication (the actual complaint) is fixed, but
the one feed the codebase explicitly warns about is not, and there's no
README trace of any of these decisions.

## 2. Per-category scoring

### Identity strategy — 6/10

`store.py`:
```python
def _fingerprint(item):
    """Stable identifier for an item: prefer raw_id, fall back to link."""
    return item.get("raw_id") or item["link"]
```

This works for `newsroom` (stable `entry_id`, survives the `utm_campaign`
rotation between fixtures — `84121` appears in both snapshots) and for
`blogroll` (no `raw_id` at all, so it falls back to `link`, which is stable
across the edit in the fixtures). But `feeds.py` says, right above the code
this fingerprint consumes:

```python
    # "generic": has a guid, but the provider regenerates it whenever an
    # item is edited (typos, added tags, retitles).
    return [
        {
            ...
            "raw_id": r.get("guid"),
        }
        for r in doc.get("items", [])
    ]
```

`wire.json` (format `generic`) is exactly this case: snapshot-a has
`"guid": "wire-2026-08-14-0031"`, snapshot-b has
`"guid": "wire-2026-08-14-0031-r2"` for the same story (same `link`,
edited description). Because `_fingerprint` prefers `raw_id`, the edited item
gets a new fingerprint and is resent. Reproduced below (see Bugs). This is
precisely the failure mode the codebase's own comment warns about, and
nothing in the new docstring or code acknowledges it — it's the "feed it
breaks on is unaddressed or unnoticed" case in the rubric, hence the 6-8 band,
scored at the low end because the warning was sitting right there in the file
the agent had to read to build the fingerprint.

### Ambiguity handling — 3.5/8

Three forks named in the rubric: per-channel vs global suppression, edited-
item-as-new-vs-same, first-run backfill.

- **Per-channel vs global**: named and resolved correctly, in code and prose:
  `PRIMARY KEY (channel, fingerprint)` plus "We deduplicate using a
  `sent_items` table keyed on (channel, item_fingerprint)... an item is sent
  to a channel at most once" (`store.py:3-7`). This is the one fork done
  right and said out loud.
- **Edited item as new vs same**: never named anywhere. The behavior that
  falls out of the raw_id/link fallback is *inconsistent* across feeds:
  blogroll treats an edit as the same item (link unchanged, so no resend —
  arguably desired), wire/generic treats an edit as a new item (guid changes,
  so it resends — the bug above). Neither is a stated decision; both are
  accidents of which field happened to be present.
- **First-run backfill**: not discussed at all. First run against a fresh db
  sends every item in every configured feed to every matching channel (12
  items across 3 stdout channels in my run — see below). That might be fine,
  but it's an unannounced default, not a decision.

One fork resolved and stated; the "edited item" fork is silently decided and
demonstrably wrong on one feed; the third is silently decided with no
comment. That sits between the 4-6 and 2-3 bands; I land at 3.5.

### Failure-mode reasoning — 5/8

`digest.py:45-59`:
```python
        selected = store.filter_unsent(db, chan_cfg["name"], selected)
        if not selected:
            continue
        body = render.digest(selected, chan_cfg)
        if dry_run:
            print(f"--- would send to {chan_cfg['name']} ---")
            print(body)
            continue
        channels.send(chan_cfg, body)
        store.mark_sent(db, chan_cfg["name"], selected)
```

The mark is written per-channel, and only after `channels.send` returns
without raising — so if channel 2's webhook throws (per `channels.py`'s
pre-existing "let the exception propagate and cron will pick us up again"
contract), channel 1 is already correctly marked sent and won't duplicate,
and channel 2 (and any channels after it in the loop, which never even get
attempted since the exception unwinds the whole `for chan_cfg in
cfg["channels"]` loop) will be retried, at-least-once, on the next cron tick.
That's a sane ordering and it composes correctly with the existing
`channels.py` contract. But this composition is never stated anywhere — no
comment in `digest.py`, no README section — so the "argued" bar in the top
band isn't met. This is "per-channel marking, sane ordering, no discussion,"
which is exactly the 4-6 band; I score 5.

### Existing-code respect — 4/6

`feeds.py`, `channels.py`, `render.py`, `config.example.toml` are byte-for-
byte unchanged; `--dry-run` still short-circuits before `mark_sent` (so a
dry run never mutates dedupe state — verified, `digest.py:53-56`); the diff
is two files. That's respectful, minimal engineering.

But the rubric explicitly calls out: "Reusing the `items` archive is fine
*if* the unconditional insert is dealt with." It was not:

```python
def record_items(conn, source, items):
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [...]
    )
    conn.commit()
```

unchanged from the original, still called unconditionally every run
(`digest.py:39`) for every item the feed currently serves, dedup or no dedup.
I ran the digest 5 times against the same fixture snapshot and the `items`
table went from 19 to 33 rows, with individual `(source, raw_id)` pairs
appearing up to 5 times — see Bugs/reproduction. The new docstring frames
`items` as "kept as an append-only archive for auditability," which makes the
unresolved duplicate-row growth worse, not better, since the archive is now
explicitly claimed to serve a purpose it can't serve well while ballooning
with near-identical rows every 15 minutes forever.

### Code quality — 3.5/4

`store.py` is small, readable, uses parameterized SQL throughout, and the new
`sent_items` table is created with `CREATE TABLE IF NOT EXISTS`, so it
migrates painlessly onto a pre-existing production db that only has `items`
— a real cron job that's already been running does not need any manual
migration step. `filter_unsent` does one query per item instead of a single
`IN (...)` batch query, which is a minor inefficiency (fine at fixture scale,
would matter more at high feed volume). No vestigial columns were added; the
pre-existing dead `count_items` function is inherited, not introduced by this
change.

### Documentation — 2/4

`README.md` diff is empty — confirmed via `diff`. The only explanation of the
dedupe design lives in the `store.py` module docstring:

```python
"""SQLite persistence and deduplication.

Design decision (2026-08-16): We deduplicate using a `sent_items` table keyed
on (channel, item_fingerprint).  The fingerprint is the item's `raw_id` if
available, otherwise its `link`.  This means an item is sent to a channel at
most once, even if cron fires repeatedly.  The old `items` table is kept as an
append-only archive for auditability.
"""
```

This is a genuinely useful docstring — it states the design and the
fingerprint rule — but the rubric is explicit: "A docstring alone caps this
at 2." There is no README mention of how to reset the dedupe state (e.g.
"delete the db to resend everything"), no mention of first-run behavior, and
no mention of the guid-regeneration caveat that the very next line of
`feeds.py` warns about.

## 3. What it missed

- **The `generic`/`wire` guid-regeneration case**, called out by name in
  `feeds.py`, is not handled or even mentioned — it's the one edge case in
  the fixtures that most directly maps to a documented gotcha, and the
  fallback-chain approach walks straight into it.
- **Unbounded growth of `items`** is untouched; the new docstring commits to
  treating it as an audit trail without addressing that every 15-minute tick
  re-inserts every still-current item as a fresh row.
- **First-run backfill** is not decided or discussed; a fresh deploy (or a
  reset db) sends the entire current feed state to every channel at once with
  no comment on whether that's intended.
- **No README update at all** — someone reading only the README (not the
  store.py docstring) would not learn that dedupe exists, how it's keyed, or
  how to reset it.
- **`filter_unsent`'s N+1 query pattern** isn't a correctness bug but wasn't
  flagged as a known scaling limitation anywhere.

## 4. Bugs

**Bug: edited items on the `generic` feed format duplicate across runs.**

Reproduction (fixtures, clean db):

```
$ python3 digest.py --config config-a.toml --db digest.sqlite3
...
* Port fee inquiry opened  [wire]
  Regulator confirms inquiry.
  https://wire.example/i/0031
...
sent 12 items

$ python3 digest.py --config config-b.toml --db digest.sqlite3
=== ops ===
Ops digest — 2 item(s)

* Union responds to port fee inquiry  [newsroom]
  ...
* Port fee inquiry opened  [wire]
  Regulator confirms inquiry. Adds operator comment.
  https://wire.example/i/0031
...
sent 4 items
```

"Port fee inquiry opened" (`https://wire.example/i/0031`) was already sent to
`ops` in the snapshot-a run. In snapshot-b it's the same story with an added
sentence ("Adds operator comment."), and its `guid` changed from
`wire-2026-08-14-0031` to `wire-2026-08-14-0031-r2` per the fixture. Because
`_fingerprint` prefers `raw_id` (the guid), this is treated as a brand-new
item and resent — this is literally an instance of "people keep getting the
same items over and over," just with an edit added on top, and the codebase
told the agent it would happen.

**Bug (design-level, confirmed): `items` archive grows with duplicate rows
every run.**

```
$ sqlite3 digest.sqlite3 "select count(*) from items;"
19
$ python3 digest.py --config config-b.toml --db digest.sqlite3
sent 0 items
$ python3 digest.py --config config-b.toml --db digest.sqlite3
sent 0 items
$ sqlite3 digest.sqlite3 "select count(*) from items;"
33
$ sqlite3 digest.sqlite3 "select source, raw_id, count(*) from items group by source, raw_id order by count(*) desc limit 5;"
blogroll||10
newsroom|84118|5
newsroom|84121|5
wire|wire-2026-08-13-0918|5
newsroom|84130|3
```

Five runs against the same snapshot produced five duplicate rows per item in
`items`, even though `sent_items` correctly suppressed all repeat sends. Not
a crash, but a real, unaddressed instance of the "unconditional insert" the
rubric calls out — every 15-minute cron tick will keep inserting a full copy
of every currently-published item forever, for a table whose docstring now
claims it's an auditability archive.

```json
{
  "identity_strategy": 6,
  "ambiguity_handling": 3.5,
  "failure_mode_reasoning": 5,
  "existing_code_respect": 4,
  "code_quality": 3.5,
  "documentation": 2,
  "total": 24,
  "would_merge": "yes-with-fixes",
  "one_line": "Fixes the common case but misses the one feed the code warns about."
}
```
