# Review: notify-digest fix (ornith-1.5-8bit, run 2)

## 1. Summary

The agent added a `delivered` table keyed by `(channel, source, key)`, computed a per-feed-format `key` in `feeds.py` chosen to survive each provider's specific way of mutating an item (rotating `utm_campaign`, missing `raw_id`, regenerating `guid`), and marks delivery only after a channel's send succeeds. It left `channels.py`, `render.py`, and `config.example.toml` untouched, preserved `--dry-run`, and rewrote the README with a dedupe section that names the per-channel and edited-item decisions and the first-run backfill behavior. I tested it against both fixture snapshots (fresh run, repeat run, edited-item run, and a simulated mid-batch channel crash) and it behaved correctly in every case; I would merge this after a cosmetic indentation fix and a one-line addition to the README about how to reset the dedupe state.

## 2. Per-category scoring

### Identity strategy — 9.5/10

`feeds.py` picks a different stable field per format and states why, in the module docstring:

```python
`key` is the stable identity used to tell "the same item, seen again" apart
from a genuinely new one. It must survive the way each provider edits items,
which is why it is chosen per format below rather than taken from `raw_id`:

  * newsroom has a stable numeric `entry_id`, but its URL is littered with
    volatile tracking params, so the id (not the link) is the key.
  * blogroll has no id at all; the permalink is the only field that survives
    an edit, so it doubles as the key.
  * generic regenerates its `guid` on every edit, so neither `raw_id` nor any
    id-like field is trustworthy; the link is stable, so that is the key.
```

And in code (`feeds.py:60-97`):

```python
"raw_id": str(r["entry_id"]),
"key": str(r["entry_id"]),
...
"raw_id": None,
"key": r["permalink"],
...
"raw_id": r.get("guid"),
"key": r["link"],
```

I verified this against both fixtures directly. `snapshot-b` regenerates the newsroom guid-equivalent (`utm_campaign=w33` → `w34`) on the *same* `entry_id`, rewrites the `wire` (generic) guid from `wire-2026-08-14-0031` to `wire-2026-08-14-0031-r2` on the same link, and edits the blogroll post title/excerpt on the same permalink. Running snapshot-a then snapshot-b through the real CLI:

```
$ python3 digest.py --config config.toml --db test.sqlite3   # snapshot-a
sent 6 items
$ python3 digest.py --config config.toml --db test.sqlite3   # snapshot-a again
sent 0 items
$ python3 digest.py --config config.toml --db test.sqlite3   # switched to snapshot-b
sent 1 items   # only the genuinely-new "Union responds to port fee inquiry" item
```

All three edited items were correctly suppressed and the one new item got through. This is a real per-feed identity strategy, stated with its failure modes, that holds up on all three feeds in the fixtures — top band. Docked half a point because the "generic" choice (link-as-key) is asserted but not defended against the case where an edit *does* change the link (a retitle-with-slug-change provider would break this silently, and that risk isn't flagged).

### Ambiguity handling — 8/8

All three forks named in the rubric are explicitly identified and resolved, in code and prose:

- **Per-channel vs global suppression** — resolved per-channel and stated why, `README.md`:
  > "Per channel — a channel that was down for a while gets its backlog when it returns, and one channel never starves another of an item it also matches."
  Backed by the schema (`store.py:32-38`): `PRIMARY KEY (channel, source, key)`.
- **Edited item as new vs same** — resolved as "same" and the rationale is stated, `README.md`:
  > "Using `raw_id` or the URL directly would re-send edited items, which is the original bug."
- **First-run backfill** — named explicitly, `README.md`:
  > "Upgrading: the `delivered` table is created empty on first run, so the first run after upgrading will re-send the current backlog once — expected."

All three are surfaced in code *and* explained in the README with reasoning, which is exactly what the top band asks for.

### Failure-mode reasoning — 5/8

Marking happens after send, per channel (`digest.py:52-64`):

```python
channel = chan_cfg["name"]
already = store.delivered_keys(db, channel)
selected = [
    i
    for i in all_items
    if matches(i, chan_cfg) and (i["source"], i["key"]) not in already
]
if not selected:
    continue
body = render.digest(selected, chan_cfg)
if dry_run:
    print(f"--- would send to {chan_cfg['name']} ---")
    print(body)
    continue
channels.send(chan_cfg, body)
store.mark_delivered(db, channel, [(i["source"], i["key"]) for i in selected])
sent += len(selected)
```

I confirmed the ordering is correct with a live test: two channels, the first `stdout` (succeeds) and the second `webhook` pointed at an unreachable port (fails and raises, killing the process per `channels.py`'s "let it propagate" contract):

```
$ python3 digest.py --config config2.toml --db test3.sqlite3
=== chan1 ===
...
channels.DeliveryError: chan2: <urlopen error [Errno 61] Connection refused>
exit: 1
$ sqlite3 test3.sqlite3 "select * from delivered;"
chan1|wire|https://wire.example/i/0031|...
chan1|wire|https://wire.example/i/0918|...
```

Only `chan1` (the channel that actually succeeded) is marked; `chan2` is not, so the next cron tick retries `chan2` only, and `chan1` is never re-sent. That is the correct at-least-once-per-channel behavior and it is genuinely per-channel, correctly ordered. What's missing is any discussion of the choice: nothing in the README or code comments states that this is deliberately at-least-once (a channel could in principle receive a duplicate if `channels.send` succeeds but the process dies before `mark_delivered`'s `conn.commit()` returns), nor does it mention the inherited crash-and-retry contract from `channels.py`'s docstring ("best effort... let cron pick us up"). Correct behavior, unargued tradeoff — solidly mid-band per the rubric.

### Existing-code respect — 6/6

`channels.py`, `render.py`, and `config.example.toml` are untouched (verified with `diff`, no output). `feeds.py`'s per-format branches are extended in place, not rewritten — the change is additive (`"key": ...` alongside the existing `"raw_id": ...`, `"source": ...` fields). The `items` archive table and its "insert everything, nothing reads it" semantics are left exactly as they were (`store.py`); the agent added a *separate* `delivered` table rather than repurposing `items` for dedup, which sidesteps the exact trap the rubric calls out ("reusing the items archive is fine if the unconditional insert is dealt with") by not reusing it for identity tracking at all. Schema migration for existing databases is handled correctly via `CREATE TABLE IF NOT EXISTS delivered (...)` — I confirmed this is additive and doesn't break an existing `digest.sqlite3` (there is none to break in the fixtures, but the DDL is non-destructive by construction). `--dry-run` was explicitly preserved and I verified it does not call `mark_delivered`:

```
$ python3 digest.py --config config.toml --db test2.sqlite3 --dry-run   # nothing printed as "sent"
$ python3 digest.py --config config.toml --db test2.sqlite3            # still sends all 6 for real
sent 6 items
```

No scope creep (no plugin system, no scheduler, no new channel types).

### Code quality — 3/4

Mostly clean, but there is a real indentation defect introduced in the "generic" branch of `feeds.py` (lines 86-97):

```python
    return [
        {
            "title": r["title"],
            "link": r["link"],
            "summary": r.get("description", ""),
            "published": r.get("pubDate", ""),
                "source": name,
                "raw_id": r.get("guid"),
                "key": r["link"],
        }
        for r in doc.get("items", [])
    ]
```

`"source"`, `"raw_id"`, and `"key"` are indented four spaces deeper than their sibling keys in the same dict literal. It's cosmetically wrong (Python doesn't care inside a dict literal, so it still runs — I confirmed `import feeds` succeeds), but it's the kind of drive-by sloppiness that shouldn't survive a review pass, especially since the surrounding two branches (`newsroom`, `blogroll`) are correctly indented. No dead code, no vestigial columns — `raw_id` stays in `items` for its stated archival purpose and isn't left over from a half-finished refactor. SQL is sensible: `INSERT OR IGNORE` into a table with a natural composite primary key, no redundant surrogate key.

### Documentation — 3.5/4

The README gained a substantial "De-duplication" section (not just a docstring) covering the mechanism, the per-channel/per-identity keying rationale with a table mapping format → key → reason, the first-run/upgrade backfill behavior, and a `--dry-run` clarification:

```
## De-duplication
...
> Upgrading: the `delivered` table is created empty on first run, so the first
> run after upgrading will re-send the current backlog once — expected.
```

This clears "explains the dedupe behaviour" and "what happens on first run" cleanly. It does not say anywhere how to force a resend / reset the dedupe state (e.g. "drop the `delivered` table," or "run `DELETE FROM delivered`"), which the rubric explicitly asks for. That gap keeps this out of a perfect 4.

## 3. What it missed

- **How to reset dedupe state.** No instruction anywhere (README or code comment) for the operationally realistic case: "channel X needs the backlog re-sent" (e.g., after a config mistake suppressed items). The schema (`delivered` keyed by `channel, source, key`) makes this a one-line `DELETE FROM delivered WHERE channel = ?`, but that's undocumented.
- **At-least-once vs at-most-once, unargued.** The implementation *is* at-least-once per channel (mark-after-send, matching `channels.py`'s existing "let it propagate, cron retries" contract), and that's the right choice given the existing crash semantics — but the agent never says so. A future maintainer has to reconstruct the guarantee by reading `digest.py:61-64` rather than being told.
- **Unbounded `delivered` growth.** `delivered_keys` loads every row ever inserted for a channel on every run (`store.py:71-75`) and the table is never pruned. For a "runs every 15 minutes forever" cron job this will grow indefinitely; not wrong, but nothing acknowledges it or suggests a retention policy (e.g., delete delivered rows for items no longer present in any feed, or older than N days).
- **Robustness of the "generic" link-as-key choice.** Silently assumes a link never changes on edit for that provider. True in the fixtures, but not defended as an assumption the way the other two formats are.

## 4. Bugs

None functional. The one defect found is cosmetic: the indentation in the `"generic"` branch of `feeds.py` (quoted above, lines 92-94) is inconsistent with its sibling dict keys and with the other two branches in the same function. It does not affect behavior — reproduced with `python3 -c "import feeds"`, which succeeds — but it's a review-blocking nit in a diff this small.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 5,
  "existing_code_respect": 6,
  "code_quality": 3,
  "documentation": 3.5,
  "total": 35,
  "would_merge": "yes-with-fixes",
  "one_line": "Correct per-feed dedup keys, well documented; minor indentation bug and no reset docs."
}
```
