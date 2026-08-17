# Review — notify-digest dedup fix

## 1. Summary

The agent added a `sent_items` table keyed on `(source, dedup_id, channel)`, a
per-channel "already sent" check before rendering, and a per-item `raw_id` fix
for the `blogroll` feed (which previously had none). This correctly stops
resends for two of the three feeds (`newsroom`, `blogroll`) but leaves the
`generic`/`wire` feed's guid-regeneration-on-edit problem — the exact failure
mode called out in `feeds.py`'s own docstring — completely unfixed, and its
`CREATE UNIQUE INDEX` migration crashes on any already-running deployment that
has accumulated duplicate rows (which, per the prompt, is exactly the state
the real cron box is in). I would not merge this as-is; it needs the wire
identity fixed and the migration made safe before it touches the production
sqlite file.

## 2. Per-category scoring

### Identity strategy — 5 / 10

`digest.py:32-39`:

```python
def _dedup_id(item):
    """Return the deduplication key for an item.

    Some feeds have no stable raw_id (blogroll) or regenerate it on edits
    (generic).  We fall back to the link URL which is the most stable
    identifier available across all providers.
    """
    return item.get("raw_id") or item["link"]
```

The comment correctly *names* both failure modes from `feeds.py`'s docstring
("blogroll: no stable identifier"; "generic: guid regenerates on edit") but
the implementation only handles the first one. `or` only falls back to
`link` when `raw_id` is falsy (`None`/`""`); for the generic/wire format
`raw_id` is always truthy after an edit, it's just a *different* truthy
string (`wire-2026-08-14-0031` → `wire-2026-08-14-0031-r2` in
`fixtures/snapshot-b/wire.json` vs `snapshot-a`). So the fallback never
triggers for the one feed whose docstring literally says "the provider
regenerates it whenever an item is edited". I confirmed this by running the
tool against snapshot-a then snapshot-b with a persistent db:

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
* Port fee inquiry opened  [wire]
  Regulator confirms inquiry.
  https://wire.example/i/0031
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a again
sent 0 items
$ sed -i '' 's/snapshot-a/snapshot-b/' config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-b, "edited"
* Port fee inquiry opened  [wire]
  Regulator confirms inquiry. Adds operator comment.
  https://wire.example/i/0031
```

Same link, same title, edited body — sent twice. This is precisely the "same
item, regenerated guid" case the rubric calls out, and it's the one case the
agent's own comment claims to solve but doesn't. The `newsroom` case (rotating
`utm_campaign` in the URL) is handled correctly because `newsroom` keys on
`entry_id`, not the URL — that part is solid, and the `blogroll` fix
(`feeds.py:66`, `"raw_id": r["permalink"]`) is a real, working fix for the
missing-id case. But one of three feeds still breaks on exactly the scenario
the codebase's own comments describe, and the agent's own docstring
misrepresents what the code does — worse than leaving it silently unnoticed.

### Ambiguity handling — 3 / 8

- **Per-channel vs. global suppression**: explicitly resolved, and stated.
  `store.py:3-9`:
  ```
  The `sent_items` table tracks which (item, channel) pairs have already been
  delivered.  This is how we avoid sending duplicates across cron runs.
  ```
  `sent_items` has a `channel` column and a `(source, raw_id, channel)`
  primary key (`store.py:36-42`), and `is_sent`/`mark_sent` are both called
  with `chan_name` (`digest.py:61,72`). This fork is named and correctly
  resolved.
- **Edited item as new vs. same**: decided silently via `_dedup_id`, and (as
  shown above) decided *wrongly* for the `wire`/generic feed while being
  decided correctly, by accident of key choice, for the other two.
- **First-run backfill**: not mentioned anywhere. On an empty/new db every
  item in the current feed snapshot is sent as "new" on the very first
  invocation — a reasonable default, but nowhere stated as a deliberate
  choice, and nowhere warned about (an operator restoring from a fresh
  `digest.sqlite3` after some incident would get a flood with no explanation
  in the README).

One fork named and correct, one decided silently and wrong, one decided
silently and unremarked — this sits at the bottom of the "one or two named"
band, docked further because the silent decision is actually broken for a
feed the codebase explicitly warns about.

### Failure-mode reasoning — 5 / 8

`digest.py:56-73`:

```python
for chan_cfg in cfg["channels"]:
    chan_name = chan_cfg["name"]
    selected = [
        i for i in all_items
        if matches(i, chan_cfg)
        and not store.is_sent(db, i["source"], _dedup_id(i), chan_name)
    ]
    if not selected:
        continue
    body = render.digest(selected, chan_cfg)
    if dry_run:
        print(f"--- would send to {chan_name} ---")
        print(body)
        continue
    channels.send(chan_cfg, body)
    for i in selected:
        store.mark_sent(db, i["source"], _dedup_id(i), chan_name)
    sent += len(selected)
```

Marking is per-channel and happens after `channels.send` succeeds, per
channel, inside the channel loop — so a channel that raises (per
`channels.py`'s documented best-effort semantics) leaves its own items
unmarked (retried next tick) while a channel that already succeeded earlier
in the same run keeps its marks (no re-send, since `mark_sent` commits
immediately). That's sane, at-least-once-leaning ordering. But there is no
comment or README text arguing the choice or naming the crash window between
`channels.send` returning and `mark_sent` committing (a crash there redelivers
that channel's batch) — it's implemented correctly but not discussed, which
is exactly the "sane ordering, no discussion" band.

### Existing-code respect — 4 / 6

The agent kept `feeds.py` almost untouched (one-line fix), didn't rewrite
`render.py` or `channels.py`, and preserved `--dry-run` (verified: the
`dry_run` branch returns before `mark_sent` is ever called, so a dry run
cannot mutate `sent_items`). It reused the `items` archive as instructed by
the rubric's guidance, and did deal with the previously-unconditional insert
(`store.py:52`, `INSERT OR IGNORE INTO items ...`). Docked for the schema
change below, which breaks exactly the existing (already-deployed, already
accumulating rows) database the prompt describes.

### Code quality — 1.5 / 4

`store.py:27-29`:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_raw_id
    ON items(source, raw_id);
```

This statement is run unconditionally on every `connect()` call
(`store.py:44-49`, `conn.executescript(SCHEMA)`), including against an
existing production db. The old `record_items` did an unconditional
`INSERT` every 15 minutes with no dedupe (that's the whole bug being fixed),
so any db that's actually been running in prod for more than one tick will
already contain duplicate `(source, raw_id)` rows. I reproduced this:

```python
# simulate a pre-fix db that's been accumulating dupes for a while
conn = sqlite3.connect('/tmp/old.sqlite3')
conn.execute("""CREATE TABLE items (... same old schema ...)""")
for _ in range(5):
    conn.execute("INSERT INTO items (source, raw_id, title, link) VALUES ('wire','abc123','t','l')")
conn.commit()
```
```
>>> store.connect('/tmp/old.sqlite3')
FAILED: IntegrityError UNIQUE constraint failed: items.source, items.raw_id
```

Deploying this patch onto the actual cron box described in the prompt — one
that has been running this exact unconditional-insert code every 15 minutes
— will crash on the very next invocation, taking down the whole digest job.
There is no migration step, no `try/except`, no dedup-then-index approach.
This is the rubric's "schema migration handled for an existing database"
item, failed outright, and it's a severe enough defect (breaks the system the
task asked to fix) that it drags code quality to the bottom of the range
despite otherwise readable code and no dead code.

### Documentation — 1.5 / 4

The README (`README.md`) is byte-for-byte unchanged from the original — no
mention of dedup, no mention of `sent_items`, no reset/backfill instructions,
no caveat about the wire feed. All the explanation lives in two docstrings
(`store.py:1-11`, `digest.py:33-38`), and one of them (quoted above under
Identity strategy) actively misdescribes what the code does for the generic
feed. Per the rubric, "a docstring alone caps this at 2"; given that the
docstring is also partly wrong, I'm scoring below that cap.

## 3. What it missed

- **First-run backfill** — not decided out loud anywhere; current behavior
  (send everything in the feed on an empty db) is defensible but unstated.
- **The generic/wire edited-item case** — noticed in a comment, not actually
  fixed; the fallback-to-link logic never triggers for it because `raw_id`
  is truthy even when it has changed value.
- **Schema migration for a live database** — no plan for upgrading an
  existing `digest.sqlite3` that already contains the duplicate rows the bug
  itself caused; the new unique index construction crashes on exactly that
  data.
- **No README update** — the person who has to run this a year from now
  gets no explanation of the new table, the new column, or how to reset a
  channel's send history (e.g. to force a re-send after a mistake), despite
  the prompt's explicit request to leave the codebase understandable to the
  next person.
- **Retry-window tradeoff for delivery marking** — implemented reasonably
  (send-then-mark, per channel) but not argued anywhere, so the next person
  has to reverse-engineer the at-least-once guarantee from the code.

## 4. Bugs

**Bug 1 — wire/generic feed items are resent whenever the provider edits them**
(regenerates the guid), which is the exact scenario `feeds.py`'s own
docstring describes. Reproduction:

```sh
cd <agent output dir>
cp config.example.toml config.toml
rm -f digest.sqlite3
python3 digest.py --config config.toml --db digest.sqlite3   # sends "Port fee inquiry opened" [wire]
python3 digest.py --config config.toml --db digest.sqlite3   # sent 0 items — correctly deduped
sed -i '' 's/snapshot-a/snapshot-b/' config.toml
python3 digest.py --config config.toml --db digest.sqlite3   # resends "Port fee inquiry opened" [wire] — BUG
```

**Bug 2 — the schema upgrade crashes on any database that has already
accumulated duplicate `(source, raw_id)` rows**, which is guaranteed for the
already-running cron deployment the prompt describes (unconditional inserts
every 15 minutes prior to this patch). Reproduction:

```python
import sqlite3
conn = sqlite3.connect('/tmp/old.sqlite3')
conn.execute("""CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, raw_id TEXT,
    title TEXT NOT NULL, link TEXT NOT NULL, summary TEXT, published TEXT,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')))""")
for _ in range(5):
    conn.execute("INSERT INTO items (source, raw_id, title, link) VALUES ('wire','abc123','t','l')")
conn.commit(); conn.close()
```
```python
import store
store.connect('/tmp/old.sqlite3')
# sqlite3.IntegrityError: UNIQUE constraint failed: items.source, items.raw_id
```

Any real deployment hitting this patch will have its cron job start failing
outright on the next tick.

```json
{
  "identity_strategy": 5,
  "ambiguity_handling": 3,
  "failure_mode_reasoning": 5,
  "existing_code_respect": 4,
  "code_quality": 1.5,
  "documentation": 1.5,
  "total": 20,
  "would_merge": "no",
  "one_line": "Fixes 2/3 feeds but crashes on any real (already-duplicated) production database."
}
```
