# Review — notify-digest, ornith-1.5-4bit, run-1

## 1. Summary

The agent added a canonical URL-based identity key (query string and fragment stripped) and a per-channel `delivered` table, so an item is now suppressed only for the channels it has already reached, marked delivered after a successful send. This is a solid, well-reasoned core fix — verified against both fixture snapshots, it correctly stops re-sending unchanged items while still surfacing a genuinely new one — but it ships with no migration path for the `items` table's new `UNIQUE`/`key` columns (a crash on the very database this is meant to run against), a botched indentation edit that leaves one code path visibly broken-looking, and zero documentation changes: the README is byte-for-byte identical to the original, so "the next person to touch it" has nothing to read except two source-file comments. I would merge with fixes, not as-is — the migration crash has to be dealt with before this touches the real `digest.sqlite3` on the cron box.

## 2. Per-category scoring

### Identity strategy — 7/10

`feeds.canonical_key` is used consistently across all three feed formats:

```python
def canonical_key(link):
    """Identity for an item: scheme + host + path, tracking params dropped."""
    parts = urlsplit(link)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"
```

Applied at `feeds.py:66` (newsroom), `feeds.py:80` (blogroll), `feeds.py:97` (generic/wire). I verified this against the fixtures: `wire.json` snapshot-b regenerates the guid (`wire-2026-08-14-0031` → `wire-2026-08-14-0031-r2`) with the same link, and it is correctly suppressed on a second run; `newsroom.json` snapshot-b rotates `utm_campaign=w33` → `w34` on unchanged articles, also correctly suppressed; the genuinely new newsroom item (`port-fees-union`) is correctly delivered. That covers exactly the two failure modes called out in the feed docstrings.

It loses points because the strategy is asserted, not argued to its edge: no handling for a link-only change (a provider correcting a typo'd URL, or moving `http://` → `https://`) which would silently create a duplicate under this key; and blogroll, which the code comments admit "has no stable identifier of any kind," gets the exact same link-based key as the other two feeds with no acknowledgment that if blogroll's permalinks are themselves reissued (a real possibility for a feed with *no* id), dedup fails silently there. The choice is stated but its own failure modes are not.

### Ambiguity handling — 5/8

Per-channel vs. global suppression is the fork the agent nailed and *documented explicitly*, unprompted:

```python
# store.py
"""
`delivered` records, per channel, which item keys we have already sent. It is
what keeps the same item from being pushed twice. Deduplication is per channel
on purpose: an item can legitimately belong to more than one channel (the
"everything" firehose matches every keyword), so a single global "seen" set
would hand each item to only the first matching channel and starve the rest.
"""
```

That's the top-band treatment for this one fork: named, resolved correctly, and the reasoning is in the code where the next person will find it.

The other two forks are decided silently. "Edited item as new vs. same": the wire item in snapshot-b has an actual content edit (`"Regulator confirms inquiry."` → `"Regulator confirms inquiry. Adds operator comment."`) and the blogroll item gets a retitle (`"Notes on port fee arithmetic"` → `"(updated)"`) — I confirmed both are silently suppressed on the second run because the link-derived key is unchanged. That's a real editorial call (do subscribers want to see corrections/updates?) made with no comment anywhere that this is what canonical-key identity implies. First-run backfill isn't addressed at all: on a fresh `digest.sqlite3`, the first cron tick will deliver every single item currently in every feed with no distinction from a "new" item — for a wire/newsroom source that could mean dozens or hundreds of already-old items firehosed to every channel the moment the file is deployed. Nothing in code or docs flags this.

### Failure-mode reasoning — 6/8

Per-channel marking, and the ordering is right for at-least-once semantics — `store.mark_delivered` is called only after `channels.send` returns without raising:

```python
# digest.py
body = render.digest(selected, chan_cfg)
if dry_run:
    print(f"--- would send to {channel} ---")
    print(body)
    continue
channels.send(chan_cfg, body)
store.mark_delivered(db, channel, [i["key"] for i in selected])
sent += len(selected)
```

Combined with `channels.py`'s existing "let the exception propagate, cron will retry" contract, this means channel 2 failing after channel 1 succeeds correctly leaves channel 1 marked and channel 2 unmarked (retried next tick) — I did not have to guess this, it follows directly from the loop shape. `mark_delivered` also uses `INSERT OR IGNORE`, so a crash mid-batch that gets partially retried won't raise on the overlap.

It stops short of top marks because none of this is *argued* anywhere — no comment in `digest.py` or the README states "we chose at-least-once over at-most-once because X," it's an artifact of loop order rather than a stated design decision. There's also a one-line comment gesturing at it (`# Delivered state is only written after a send succeeds, so a failed delivery retries on the next cron tick.`) but that's describing the mechanism, not weighing the tradeoff.

### Existing-code respect — 3/6

The agent worked inside the existing shape — `feeds.py`'s three-format branching, `channels.py`, `render.py`, and `--dry-run` are all untouched and still function (verified: `--dry-run` still prints without touching the DB). The `items` archive is extended rather than replaced, and made idempotent via `UNIQUE(source, key)` + `INSERT OR IGNORE` instead of left as the unconditional insert the original had, which the rubric explicitly credits.

But it loses points for two real problems. First, a genuine schema-migration failure: the original `items` table has no `key` column and no `UNIQUE` constraint; `CREATE TABLE IF NOT EXISTS` in `store.py:24-35` is a no-op against any pre-existing database, and the very first write after upgrade crashes:

```
$ python3 digest.py --config config.example.toml --db old.sqlite3
sqlite3.OperationalError: table items has no column named key
```

(Reproduced against a hand-built copy of the *original* schema — see "Bugs" below.) Given the prompt is literally "we run this from cron," there is an existing `digest.sqlite3` on that box, and this patch as written will take the job down the next time it runs, not fix it. Second, a sloppy in-place edit left visible damage in `feeds.py` (see Code quality below) — not a rewrite, but not respectful craftsmanship either.

### Code quality — 2/4

The dedup logic itself (`canonical_key`, `delivered_keys`, `mark_delivered`) is clean and readable. But the edit to the "generic" feed branch in `feeds.py` broke the existing indentation and left the block looking like a merge artifact:

```python
    return [
        {
            "title": r["title"],
            "link": r["link"],
                "summary": r.get("description", ""),
                "published": r.get("pubDate", ""),
                "source": name,
                "raw_id": r.get("guid"),
                "key": canonical_key(r["link"]),
            }
        for r in doc.get("items", [])
    ]
```

(`feeds.py:89-99`.) It's syntactically valid Python (dict-literal indentation is cosmetic), but it is objectively worse than what was there before the agent touched it, and it's exactly the kind of thing "the next person to touch it" trips over. No dead code otherwise, and no vestigial columns were introduced, but the schema-migration gap above is also a code-quality issue (sensible SQL that only works against a schema that will never again exist once this ships).

### Documentation — 1/4

`README.md` is byte-for-byte identical to the original — I diffed it directly and there is no output. The only place any of this is explained is two module docstrings (`feeds.py:11-18`, `store.py:5-19`) and one inline comment in `digest.py`. Per the rubric, "a docstring alone caps this at 2"; here it's not even a case of the docstring covering everything a README-reader needs (no mention of first-run behavior, no mention of how to reset dedup state — e.g. drop the `delivered` table or point `--db` at a fresh file — anywhere at all), so it sits below that cap. The task's own closing instruction — "leave the codebase in a state where the next person to touch it understands what you chose and why" — is the one line of the prompt this run visibly did not act on outside the source files it happened to be editing anyway.

**Category total: 7 + 5 + 6 + 3 + 2 + 1 = 24 / 40**

## 3. What it missed

- **First-run backfill.** Never named, never handled. Deploying this against feeds with existing history will digest everything on tick one.
- **Editorial edits vs. new items.** The link-based key silently treats a corrected/expanded article as "already sent" (confirmed against snapshot-b's wire and blogroll edits). This is a real, defensible choice, but it's never surfaced as a choice.
- **Resetting dedup state.** No documented way to say "resend everything for channel X" or "this channel's history should be cleared" — an operator would have to know to `DELETE FROM delivered WHERE channel = ?` by reading the schema themselves.
- **Schema migration for the live database.** Silently assumed a fresh DB. Never stated, never handled, and it's the single biggest gap given the prompt describes an already-running cron job.
- **The at-least-once/at-most-once tradeoff.** Correctly implemented via ordering, never argued.

## 4. Bugs

**Migration crash against an existing database.** Reproduction:

```
$ python3 - <<'EOF'
import sqlite3
conn = sqlite3.connect("old.sqlite3")
conn.executescript("""
CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    raw_id TEXT,
    title TEXT NOT NULL,
    link TEXT NOT NULL,
    summary TEXT,
    published TEXT,
    first_seen TEXT NOT NULL DEFAULT (datetime('now'))
);
""")
conn.commit()
EOF
$ python3 digest.py --config config.example.toml --db old.sqlite3
Traceback (most recent call last):
  ...
  File "store.py", line 61, in record_items
    conn.executemany(
sqlite3.OperationalError: table items has no column named key
```

This is the original schema (pre-patch) with one row's worth of prior state — exactly what `/srv/notify-digest/digest.sqlite3` looks like on the box this cron job actually runs on. Deploying this patch as-is takes the job down on its first tick, which is a direct contradiction of the task ("people keep getting the same items... fix it") — it doesn't just fail to dedupe, it stops sending anything at all.

**Cosmetic but real:** the misindented dict literal in `feeds.py:89-99` (quoted above under Code quality) — not a runtime bug, but a readability regression introduced by this patch in a file the agent was already editing carefully elsewhere.

```json
{
  "identity_strategy": 7,
  "ambiguity_handling": 5,
  "failure_mode_reasoning": 6,
  "existing_code_respect": 3,
  "code_quality": 2,
  "documentation": 1,
  "total": 24,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid per-channel key-based dedupe, but no DB migration and zero README update."
}
```
