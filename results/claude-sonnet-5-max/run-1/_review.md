# Review: notify-digest dedupe fix

## 1. Summary

The agent correctly diagnosed the root cause (the `items` archive was write-only, so nothing ever
gated resends) and fixed it with a per-feed-format `dedupe_key` computed in `feeds.py`, checked
against a new `UNIQUE`-indexed column in `store.py` before anything is considered for a digest.
It handles all three fixture identity traps (rotating `utm_campaign`, no `raw_id` at all, and a
regenerated `guid`) correctly, adds a schema migration for existing databases, and writes an
unusually thorough README/docstring trail explaining the choices — including one it got wrong in
spirit: the "sent" marker is written once per run, after all channels, not per channel, which the
README admits duplicates a whole batch on partial delivery failure. I would merge this
(yes-with-fixes): the core dedupe logic is well-reasoned and verified against the fixtures, but the
failure-mode handling should be moved to per-channel/per-item marking before this goes back into
production cron.

## 2. Per-category scoring

### Identity strategy — 9.5 / 10

The agent picked a per-format fallback identity instead of a single global rule, and stated the
failure mode of each choice. From `feeds.py`:

```python
def _dedupe_key(source, fmt, item):
    if fmt == "newsroom":
        return f"{source}:id:{item['raw_id']}"
    return f"{source}:link:{_canonical_link(item['link'])}"
```

with `_canonical_link` stripping query/fragment specifically because of the `utm_campaign` churn:

```python
def _canonical_link(link):
    parts = urllib.parse.urlsplit(link)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
```

I verified this against both fixture snapshots (see the reproduction under "Bugs" — no bugs found,
just confirming the behavior):

```
$ python3 digest.py --config config.toml --db digest.sqlite3          # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3          # same snapshot again
sent 0 items
$ sed -i '' 's/snapshot-a/snapshot-b/' config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3
sent 2 items
* Union responds to port fee inquiry  [newsroom]
```

Only the genuinely new item ("Union responds...") went out; the newsroom item whose URL gained a
new `utm_campaign`, the blogroll item that was edited with no id at all, and the wire item whose
`guid` was regenerated all stayed suppressed. `test_digest.py::test_snapshot_b_only_sends_the_genuinely_new_item`
encodes exactly this and passes. Docked half a point only because the reasoning, while written
down per-format, doesn't discuss what happens if a provider changes its format string for an
existing feed name (unlikely, but the namespacing comment implies the author considered
collisions between feeds, not within one over time).

### Ambiguity handling — 6 / 8

Two of the three canonical forks are named explicitly and well-argued:

- **Per-channel vs. global suppression** — named and justified in README.md:
  > "**"Seen" is global, not per-channel.** An item is recorded once it's been through a run,
  > regardless of which channels (if any) it matched. If you edit a channel's `keywords` later,
  > items already seen under the old filter will not retroactively appear..."
- **Edited item as new vs. same** — this *is* the entire `_dedupe_key` design, discussed at
  length per format in both `feeds.py`'s module docstring and the README table.

The third fork — **first-run backfill** — is never named. The behavior (first run with an empty
DB sends everything currently in the feed windows) is the obvious/correct choice and is covered by
`test_first_poll_sends_all_six_items`, but nothing in the README or code comments acknowledges that
this is a decision at all — e.g. that a fresh deploy against feeds with a long "current window"
will flood every channel with a backlog on the very first tick. That's a real operational surprise
for whoever deploys this that the agent didn't flag.

### Failure-mode reasoning — 3 / 8

The reasoning is articulate and the tradeoff is explicitly argued, but the actual mechanism
implemented is **per-run, not per-channel**, marking — one `store.record_items` call after the
entire channel loop in `digest.py`:

```python
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
    ...
    if not dry_run:
        store.record_items(db, new_items)
```

I reproduced the exact scenario the README describes — channel 1 succeeds, channel 2 fails:

```
$ python3 - <<'EOF'
cfg = {"feeds": [...one feed...],
       "channels": [{"name":"chanA","type":"stdout", ...},
                     {"name":"chanB","type":"webhook","url":"http://localhost:1/x", ...}]}
db = store.connect("digest.sqlite3")
digest.run_once(cfg, db)
EOF
=== chanA ===
A — 2 item(s)
...
EXCEPTION: chanB: <urlopen error [Errno 61] Connection refused>
count in db: 0
```

`record_items` never runs (the exception from `channels.send` propagates out of `run_once`
before reaching it), so **nothing** is marked seen — including the two items chanA already
received. Re-running with just chanA confirms the duplicate:

```
$ python3 - <<'EOF'   # same db, chanB removed from config
digest.run_once(cfg, db)
EOF
=== chanA ===
A — 2 item(s)          # <- the exact same two items, delivered again
```

This is exactly the rubric's 2–3 band: "per-run marking, or a partial-failure window that loses
or duplicates a whole batch." The agent's own README documents this precisely and honestly:
> "If channel 2 of 3 fails, channel 1 (which already succeeded) gets a duplicate on the retry."

Credit for correctly reasoning about at-least-once vs. at-most-once and choosing not to lose
items — but the design explicitly documented is the weaker of the two implementations the rubric
distinguishes, and it duplicates the *entire* batch across *every* channel on any single channel's
failure, not just the failing channel's items. A per-channel sent-table (channel, dedupe_key) would
have gotten this into the 7–8 band with the same argued tradeoff.

### Existing-code respect — 5.5 / 6

The agent worked with the grain of the code rather than rewriting it. `feeds.py`'s per-format
parsing blocks are untouched line-for-line; it only added `_canonical_link`, `_dedupe_key`, and a
loop to attach `dedupe_key` to each item. `channels.py` and `render.py` are untouched entirely.
The archive/dedup dual-role of the `items` table (called out in the original docstring) is
preserved and extended rather than replaced. The previously-unconditional insert is fixed cleanly
with `INSERT OR IGNORE` plus a `UNIQUE` index:

```python
def record_items(conn, items):
    conn.executemany(
        "INSERT OR IGNORE INTO items"
        " (source, dedupe_key, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ...
```

`--dry-run` behavior is preserved and explicitly tested
(`test_dry_run_preview_does_not_consume_items`), confirmed passing. Half a point off because
`store.record_items`'s signature changed (dropped the separate `source` argument in favor of
reading `item["source"]`), which is a reasonable simplification but is an unannounced API change
to a function whose original signature callers elsewhere in a larger codebase might depend on —
worth a one-line note in the docstring, which it doesn't get.

### Code quality — 4 / 4

Clean, no dead code, the migration is handled with a real `PRAGMA table_info` check rather than a
blind `ALTER TABLE` that would crash on already-migrated databases:

```python
def _ensure_dedupe_key_column(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "dedupe_key" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN dedupe_key TEXT")
```

I verified this against a hand-built "legacy" (pre-fix schema) database and it migrated without
error, preserving the two pre-existing rows. SQL is sensible (single `UNIQUE` index, `IN (...)`
batched lookup in `filter_unseen` rather than N+1 queries). No vestigial columns — `raw_id` is
kept for archival/debugging purposes and is explicitly noted as no longer the dedupe key.

### Documentation — 3 / 4

The README (`README.md`) has a dedicated "Dedup" section explaining the bug, the fix, and a table
of per-format identity reasoning that matches the code exactly. It also names the two deliberate
scope decisions (global vs. per-channel suppression, and the failure-mode tradeoff) in prose,
which is well above a docstring-only writeup. Docked a point because:
- It never says how to reset the dedup state (e.g., "delete `digest.sqlite3`" or an equivalent
  flag) — grepping the README for "reset" turns up nothing.
- "What happens on first run" is not discussed in prose at all (see Ambiguity handling above) —
  only inferable from a test name.

## 3. What it missed

- **First-run backfill flood.** Never named as a decision. On a fresh deploy, or after a
  '"reset"' (which also isn't documented — see below), every item currently sitting in a feed's
  window is sent as if new, all at once. For feeds with a long visible window this could be a
  large surprise burst to every channel. Worth at least a comment/README line.
- **How to reset/replay dedup state.** No documented way to say "resend everything" or "resend
  this one item." The obvious mechanism (delete the sqlite file, or `DELETE FROM items`) is never
  written down, despite the rubric explicitly asking for it.
- **Per-channel sent tracking.** The single global `items` archive means the failure-mode
  tradeoff (see above) affects the *entire* run's item set on any single channel error, not just
  the failing channel. A `(channel, dedupe_key)` sent-ledger was within easy reach given the
  existing schema shape and wasn't attempted; the agent instead argued for and documented the
  weaker global-batch semantics.
- **Intra-batch duplicate identity.** `filter_unseen` only checks against the database, not
  against duplicates *within* a single fetch batch. If a feed ever emits the same item twice in
  one poll (doesn't happen in the fixtures, but nothing prevents it upstream), both copies would
  be sent in the same digest before `INSERT OR IGNORE` quietly collapses them in the archive.
  Not tested, not mentioned.

## 4. Bugs

None found that are outright broken — the one apparent issue investigated (batch-duplicate on
partial channel failure, reproduced above) is a documented design tradeoff, not a silent defect,
and is scored under Failure-mode reasoning above rather than here. All four of the agent's own
tests pass, and manual runs against both fixture snapshots and a synthesized legacy database
behaved exactly as documented.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 3,
  "existing_code_respect": 5.5,
  "code_quality": 4,
  "documentation": 3,
  "total": 31,
  "would_merge": "yes-with-fixes",
  "one_line": "Excellent per-format dedupe keys; ship it, but fix per-run (not per-channel) sent-marking first."
}
```
