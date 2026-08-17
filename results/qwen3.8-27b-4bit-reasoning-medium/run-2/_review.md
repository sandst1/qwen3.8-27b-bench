# Review: notify-digest dedupe fix

## 1. Summary

The agent added a `sent(channel, link_key)` table and gated delivery on it, chose a canonical-link identity (strip query/fragment, lower-case scheme/host) as the item key with a documented rationale for why each feed's own id is unreliable, and wrote an in-place schema migration plus a substantial README section explaining the dedupe model, its failure semantics, and how to test it against the two fixture snapshots. It left `feeds.py`, `channels.py`, `render.py`, and the fixtures untouched, and `--dry-run` still works. I would merge this with one fix: the README makes a false claim about `--dry-run` recording nothing, which it does not.

## 2. Per-category scoring

### Identity strategy — 9.5/10

`store.py` picks canonical link as the identity, with an explicit, feed-by-feed justification:

```python
def item_key(item):
    """Stable identity for one item, used to dedupe across cron runs.

    We deliberately key on the canonical link rather than the feed's own id,
    because no provider id is reliable:

      * newsroom rewrites the utm_* query params on every snapshot, so the
        raw URL (and any key built from it) changes for the same story;
      * blogroll has no identifier at all (raw_id is None);
      * wire regenerates its guid whenever an item is edited.
    ...
    """
    link = item.get("link")
    if link:
        return canonical_link(link)
    if item.get("raw_id"):
        return "id:" + str(item["raw_id"])
    return "title:" + (item.get("title") or "")
```

```python
def canonical_link(link):
    parts = urlsplit((link or "").strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))
```

I ran it against the actual fixtures (fresh db, `snapshot-a`, then same db against `snapshot-b`):

```
=== RUN 3 with snapshot-b, same db ===
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
  https://newsroom.example/2026/08/port-fees-union?utm_source=feed&utm_campaign=w34
sent 2 items
```

Only the genuinely new story ("Union responds...") went out. The newsroom item with a rotated `utm_campaign`, the wire item with a regenerated guid, and the edited blogroll post were all correctly suppressed — exactly what the fixtures are built to test. The half-point deduction is for the title fallback: two different stories with the same title from a hypothetical no-link feed would collide under `"title:" + title`, and this isn't flagged as a real risk (it's labeled "hypothetical" in the docstring, which is honest, but the comment undersells that it's a weaker guarantee than the primary key).

### Ambiguity handling — 7.5/8

All three named forks are addressed:

- **Per-channel vs global suppression** — named and argued in `digest.py`:
  ```python
  # Dedupe per channel: skip anything this channel already received.
  # Per channel (not global) because one item can match several
  # channels and each audience should get it exactly once.
  already = store.sent_keys(db, chan_cfg["name"])
  ```
  and verified: `sent` is keyed `PRIMARY KEY (channel, link_key)`, so an item matching both `ops` and `everything` is correctly delivered to both once each — I confirmed this in the run-1 output above (the port-fee item appears in both `ops` and `everything`).

- **Edited item as new vs same** — this is the identity strategy itself, and it's explicit: canonical link survives edits (title change, guid rotation, utm rotation), so an edited item is treated as the *same* item, not resent. Stated directly in the README table.

- **First-run / backfill** — handled for the *upgrade* case with real care (`store._migrate`'s docstring below), but a brand-new deployment with no existing db at all isn't separately called out — it's a natural consequence of `sent` starting empty (send everything once), left implicit rather than stated as a deliberate decision. This is why I don't give full marks here.

### Failure-mode reasoning — 7.5/8

Per-channel marking, correct ordering, and the guarantee is argued, not just implemented:

```python
channels.send(chan_cfg, body)
# Record only after a successful send, so a down channel is retried
# on the next tick instead of silently dropping those items.
store.mark_sent(db, chan_cfg["name"], [store.item_key(i) for i in selected])
sent += len(selected)
```

I verified this directly with a two-channel config where the second channel is an unreachable webhook:

```
=== ops ===
...
Traceback ...
channels.DeliveryError: broken: <urlopen error [Errno 61] Connection refused>
exit: 1
[{'channel': 'ops', 'link_key': '...port-fees', ...}, {'channel': 'ops', 'link_key': '...offshore-tender', ...}]
```

`ops` (which succeeded before the crash) is marked sent; `broken` isn't, and will get the same items again next tick — an explicit at-least-once choice, stated in the README ("marks them as sent *after* the send succeeds, so a down channel is retried on the next tick instead of dropping the items") and its concurrency caveat is also named: "Two concurrent runs could both decide an item is new and both send it." That's the tradeoff argued, which is what separates 7-8 from 4-6. Held at 7.5 rather than 8 because the discussion of the digest.py crash mid-loop (a Python exception from `channels.send` propagating and killing the whole run rather than being caught and letting later channels still get their digests) is implicit in the code but not explicitly called out as a design choice anywhere.

### Existing-code respect — 5.5/6

`feeds.py`, `channels.py`, `render.py`, `config.example.toml`, and the fixtures are byte-identical to the original (verified with `diff`, no output). The pre-existing `items` archive's unconditional-insert problem is dealt with, not ignored:

```python
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_link_key ON items(link_key);
...
def record_items(conn, source, items):
    """Archive each distinct item once; a no-op for items already archived."""
    conn.executemany(
        "INSERT OR IGNORE INTO items"
        " (source, link_key, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)", ...
```

`--dry-run` still functions (verified) and a migration path is provided for existing on-disk databases (verified below, under Bugs — it works correctly). The half-point deduction is for a new inaccuracy introduced into the docs about `--dry-run`'s side effects (see Bugs) — the code wasn't touched carelessly, but the description of what it does was.

### Code quality — 4/4

Docstrings are accurate and load-bearing rather than decorative, SQL is sensible (`INSERT OR IGNORE`, composite `PRIMARY KEY (channel, link_key)`, a real migration instead of a `DROP TABLE`), and there's no dead code or vestigial columns — `raw_id` is still stored (useful for the archive/debugging) but no longer used for identity, and that's explained rather than left as an unexplained leftover column.

### Documentation — 3/4

The README gained a full "Duplicates: how they are prevented" section with a per-feed table, migration behavior, and an explicit fixture-based test recipe:

> A good end-to-end test of the dedupe: run once against `snapshot-a` with a fresh db, then again against `snapshot-b` with the *same* db — only the genuinely new story ("Union responds to port fee inquiry") should be sent on the second run.

I followed this recipe verbatim and it reproduces exactly as described. That's well above a docstring-only bar. The deduction is for a factual error introduced in the same README (see Bugs): `--dry-run` is claimed to be side-effect-free, and it isn't.

## 3. What it missed

- **Brand-new deployment (no db file at all) isn't named as its own case.** The migration docstring discusses "first run after an upgrade" in detail, but a fresh `rm digest.sqlite3 && python3 digest.py` on day one gets the same "send everything currently listed, once" behavior without it being called out as a deliberate decision for that scenario specifically.
- **Concurrent cron overlap** is named as a caveat ("Two concurrent runs could both decide an item is new and both send it") but not mitigated (no lock file, no `BEGIN IMMEDIATE` transaction around the read-then-mark). Given the task is literally "runs from cron every 15 minutes," a slow run overlapping the next tick is a realistic failure mode that's flagged but left unaddressed. This is a reasonable scope decision (documented, not silent) but is worth a next-person's attention.
- **The exception-propagation behavior across channels** — if channel 2 of 3 throws, channel 3 never runs (the exception isn't caught in the loop; it propagates out of `main`). This is consistent with the existing `channels.send` contract ("we let the exception propagate and cron will pick us up again on the next tick") and is arguably fine, but it means a single flaky channel starves every channel *after* it in `cfg["channels"]` order on that tick, and this ordering-dependent asymmetry isn't mentioned anywhere.

## 4. Bugs

**README claims `--dry-run` "records nothing," but it does write to the `items` archive table.** In `digest.py`:

```python
for feed_cfg in cfg["feeds"]:
    ...
    store.record_items(db, feed_cfg["name"], items)   # <-- runs unconditionally
    all_items.extend(items)
...
    if dry_run:
        print(f"--- would send to {chan_cfg['name']} ---")
        print(body)
        continue
```

`record_items` is called before the `dry_run` branch and is not itself conditional on `dry_run`. Reproduction:

```
$ rm -f dry.sqlite3
$ python3 digest.py --config config-a.toml --db dry.sqlite3 --dry-run > /dev/null
$ python3 -c "
import store
conn = store.connect('dry.sqlite3')
print('items:', store.count_items(conn))
print('sent rows:', conn.execute('SELECT COUNT(*) FROM sent').fetchone()[0])
"
items: 6
sent rows: 0
```

Six rows get archived into `items` on a "dry" run against a fresh db. This is *pre-existing* behavior (the original `digest.py` also calls `store.record_items` unconditionally before checking `dry_run`), so the agent didn't introduce the behavior — but it did introduce the new, incorrect claim in the README ("It records nothing, so it is safe to run as often as you like") where the original README made no such claim at all. Because `items` inserts are idempotent (`INSERT OR IGNORE` keyed on `link_key`) and `items` doesn't drive dedupe, the practical impact is low (a dry run against fixtures/production data will populate/pollute the archive table, but repeated dry-runs on the same data are harmless no-ops after the first). Still, it's a factual inaccuracy in exactly the section a future maintainer would trust to reason about safety.

No other bugs found — the identity function, per-channel dedupe, migration, and partial-failure-then-retry behavior all reproduced correctly under direct testing.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 7.5,
  "failure_mode_reasoning": 7.5,
  "existing_code_respect": 5.5,
  "code_quality": 4,
  "documentation": 3,
  "total": 37,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid canonical-link dedupe with argued tradeoffs; one false claim about --dry-run in the README."
}
```
