# Review — notify-digest, run-4

## 1. Summary

The agent introduced a `feeds.item_identity()` helper that picks identity per feed format (stable `entry_id` for `newsroom`, tracking-stripped permalink for `blogroll`/`generic`), added a `deliveries` receipts table keyed by `(channel, source, identity)` that is written only after a channel's send succeeds inside a `BEGIN IMMEDIATE` transaction, and wrote an unusually thorough README plus a 10-case test suite — all of which I verified pass and behave as described by actually running the CLI against `fixtures/snapshot-a` then `snapshot-b`. It correctly suppresses the two feed traps planted in the fixtures (the wire feed's GUID-rotation-on-edit, the newsroom feed's weekly `utm_campaign` rotation) while still delivering the one genuinely new item in snapshot-b, and it survives a mid-run channel failure without re-sending to the channel that already succeeded. I would merge this with one follow-up: the `items` archive table — which the rubric explicitly calls out — is still inserted into unconditionally every 15-minute tick with no pruning, so it grows without bound forever, exactly the thing the task's existing-code-respect criterion warns about.

## 2. Per-category scoring

### Identity strategy — 9.5 / 10

`feeds.py:24-41`:

```python
def item_identity(item, feed_cfg):
    """Stable within a source; edits are not new notifications.

    Only newsroom promises stable IDs. Other providers need a permalink;
    discard known tracking parameters, but keep article-identifying queries.
    Prefixes keep ID and URL identities in separate namespaces.
    """
    if feed_cfg.get("format", "generic") == "newsroom" and item.get("raw_id") is not None:
        return "id:" + str(item["raw_id"])
    url = urlsplit(item["link"])
    query = sorted(
        (key, value)
        for key, value in parse_qsl(url.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    )
    return "url:" + urlunsplit(
        (url.scheme.lower(), url.netloc.lower(), url.path, urlencode(query), "")
    )
```

This is a genuine fallback chain, and it is format-aware rather than naive. I ran it against both fixture snapshots to check all three traps:

- **Rotating `utm_campaign`** (`newsroom`, `entry_id` 84121/84118 keep the same ID across `w33`→`w34`): handled by trusting `raw_id` for `newsroom` only.
- **Missing `raw_id`** (`blogroll` always has `raw_id: None`): falls through to the URL branch, using `permalink`.
- **Regenerated `guid`** (`wire`/`generic`, `wire-2026-08-14-0031` → `wire-2026-08-14-0031-r2` on edit, per the original `feeds.py` docstring comment "the provider regenerates it whenever an item is edited"): the code **never uses `raw_id` for non-newsroom formats at all**, so the edited wire item lands on the same link-based identity and does not resurface. That is the deliberate trap in the fixtures and it is the one most naive dedup implementations (prefer-`raw_id`-else-link) would fall into; this solution avoids it by design, not by luck.

Live proof (full run, not the mocked test suite):

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a again
sent 0 items
$ python3 digest.py --config config-b.toml --db digest.sqlite3 # snapshot-b
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
=== everything ===
Firehose — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```

Only the one truly-new newsroom entry (84130) is delivered; the edited wire item and the edited blogroll item are correctly suppressed.

The failure mode is stated, not just implemented — README.md:58-61:

> Title, summary, and publication-time edits do not create new identities. We notify once per article, not once per revision. [...] URL-based feeds changing an article's actual permalink will look new; we do not guess using title matches.

Docked half a point only because the URL normalisation doesn't canonicalise trailing slashes or path case, which isn't exercised by the fixtures but is a real residual gap in an otherwise excellent fallback chain.

### Ambiguity handling — 8 / 8

All three forks named in the rubric are explicitly identified and resolved in README.md, not left to be inferred from code:

- **Per-channel vs. global suppression** → per-channel, stated at README.md:48: "There are now separate `deliveries` receipts keyed by **channel name, source name, and stable item identity**." Verified: `test_filter_changes_and_new_channel_have_independent_history` and my own manual failure-injection run (below) both show channel B's receipt is untouched by channel A's send.
- **Edited item as new vs. same** → same, stated at README.md:58-59 (quoted above), with the explicit caveat about the one case where this is wrong (real permalink migration).
- **First-run backfill** → send-everything-once, stated at README.md:94-97: "**The first live run after upgrading sends all current matches once**, including items people may already have received. [...] Run `--dry-run` first to preview this initial batch." This is also exercised by `test_existing_archive_is_not_treated_as_delivery`, which I re-ran and confirmed passes.

This hits the top band cleanly: all three forks named, and each resolution comes with its own stated failure mode rather than a flat assertion that it's "correct."

### Failure-mode reasoning — 7.5 / 8

Per-channel marking, after-send ordering, and the tradeoff are all there — digest.py:48-66:

```python
for chan_cfg in cfg["channels"]:
    with db:
        db.execute("BEGIN IMMEDIATE")
        selected = store.pending_items(
            db, chan_cfg["name"], [i for i in all_items if matches(i, chan_cfg)]
        )
        if not selected:
            continue
        body = render.digest(selected, chan_cfg)
        if dry_run:
            print(f"--- would send to {chan_cfg['name']} ---")
            print(body)
            continue
        channels.send(chan_cfg, body)
        store.record_deliveries(db, chan_cfg["name"], selected)
        sent += len(selected)
```

Receipts are only written after `channels.send()` returns, and each channel's `BEGIN IMMEDIATE`/commit is independent, so a failure in channel 2 cannot erase channel 1's already-committed receipt. I verified this live, outside the test suite:

```python
# energy fails; ops already succeeded
calls so far: ['ops', 'energy']
{'channel': 'ops', 'source': 'blogroll', 'identity': 'url:https://blog.example/port-fee-arithmetic'}
{'channel': 'ops', 'source': 'newsroom', 'identity': 'id:84121'}
{'channel': 'ops', 'source': 'wire', 'identity': 'url:https://wire.example/i/0031'}
# retry: ops is NOT re-sent, energy+everything are
sent 9
['energy', 'everything']
```

The at-least-once choice is argued, not just implemented — README.md:84-88:

> This favors retry over silent loss. A webhook may accept a request and then time out, or the process may die after sending but before committing its receipts. Either can produce a duplicate on retry. Exactly-once external delivery needs receiver-supported idempotency [...] Marking before sending would instead lose notifications on failure.

The half-point deduction: when channel N raises, the exception propagates straight out of `run_once`'s `for chan_cfg in cfg["channels"]` loop, so **every channel listed after the failing one is skipped for that entire run**, not just delayed — I confirmed this directly:

```
configured channel order: ['ops', 'energy', 'everything']
channels attempted this run: ['ops', 'energy']   # 'everything' never even tried
```

`everything` had nothing to do with `energy`'s failure and would have succeeded, but it has to wait a full cron tick. This is a real, slightly surprising consequence of the per-channel-transaction design and it's never mentioned in the "Delivery and failure tradeoffs" section, which otherwise reads like it's trying to be exhaustive.

### Existing-code respect — 4.5 / 6

The diff is minimal and surgical: `channels.py` and `render.py` are untouched, `feeds.py` gained one function and nothing else, `--dry-run` still works and was clarified rather than broken (README.md:13-14), and the schema addition is an idempotent `CREATE TABLE IF NOT EXISTS deliveries (...)` appended to the existing `SCHEMA` string (store.py:24-30) — a correct, no-fuss migration path for a database that already exists in production.

The deduction is for the one thing the rubric names explicitly: *"Reusing the `items` archive is fine if the unconditional insert is dealt with."* It is not dealt with — `digest.py:39` still calls `store.record_items(db, feed_cfg["name"], items)` unconditionally on every single poll, inserting a fresh row for every fetched item whether or not it was seen on the previous tick 15 minutes ago. I confirmed this by running the CLI four times against alternating snapshots:

```
total rows in items: 26
('https://wire.example/i/0918', 4)
('https://wire.example/i/0031', 4)
('https://blog.example/port-fee-arithmetic', 4)
('https://blog.example/offshore-tender-history', 4)
...
```

Every unchanged item got a fresh row on every run. Run this job every 15 minutes for a year against feeds with a handful of items each and `items` will have hundreds of thousands of near-duplicate rows, with no retention policy, dedup-on-insert, or even a mention of the tradeoff anywhere in the README. The agent clearly thought about this table — it rewrote its docstring (store.py:3-5: "fetching or previewing an item must not suppress it") — but that justification is about *why* `items` shouldn't gate delivery, not about the fact that it still silently grows unbounded. This is exactly the "unconditional insert" the rubric flags, and it was carried forward unexamined.

### Code quality — 3.5 / 4

Clean, readable, idiomatically normal Python; no vestigial columns (the new `sent_at` column is useful, not decorative); the migration is handled sensibly as noted above. Minor deduction: `store.pending_items` (store.py:58-66) runs one `SELECT` per item in a Python loop rather than a single batched query —

```python
def pending_items(conn, channel, items):
    return [
        item for item in items
        if conn.execute(
            "SELECT 1 FROM deliveries WHERE channel = ? AND source = ? AND identity = ?",
            (channel, item["source"], item["identity"]),
        ).fetchone() is None
    ]
```

— fine at fixture scale, but combined with the unbounded `items` growth above, "sensible SQL" is only partially met.

### Documentation — 4 / 4

The README is the strongest part of this submission and clears the top band on its own terms, not merely via a docstring. It has a dedicated "Why items no longer repeat" section naming the mechanism, a "Delivery and failure tradeoffs" section arguing the at-least-once choice, and an "Existing databases and rollout" section that specifically says what happens on first run against an old database (README.md:92-97, quoted above) and how to reset history (README.md:66-67: "Renaming a channel or source starts a new history namespace; keep names stable..."). It is let down only by never mentioning the `items` growth issue found above — but the rubric's documentation criterion is about dedupe/reset/first-run, all of which are covered thoroughly and correctly, so I'm not re-docking it here (the growth issue is already scored under existing-code-respect).

## 3. What it missed

- **Unconditional `items` insert** (see existing-code-respect above) — the single biggest silent decision. The agent repurposed `items` as a pure "observation archive" and documented that choice, but never flagged that this means unbounded row growth with no pruning, VACUUM, or retention note.
- **Later-channel skip on earlier-channel failure** (see failure-mode-reasoning above) — channels after a failing one are not attempted at all in that run, a consequence of letting the exception propagate out of the `for chan_cfg` loop. Not wrong, but undocumented, and the README's failure section reads as if it already covers every corner.
- **Write lock held during `--dry-run`.** `digest.py:52-53` executes `BEGIN IMMEDIATE` on every channel iteration regardless of `dry_run`, so a preview run takes the same write lock as a real send and can make a concurrently-running real cron tick wait or hit `database is locked`. Harmless in practice (the preview immediately commits a no-op) but not what "dry run" usually implies, and it's not called out.
- **No URL canonicalisation beyond tracking params** — trailing slashes, `www.` prefixes, percent-encoding case, and default ports are not normalised in `item_identity`. Not exercised by the fixtures, so not a bug today, but a real-world feed is more likely to vary on these than the `utm_*`/`fbclid`/`gclid` params the agent specifically anticipated.

## 4. Bugs

I did not find anything outright broken. The test suite (10 tests, all passing under the agent's own `python3 -m unittest -v`) and my own out-of-band manual runs against both fixture snapshots and a hand-written failure-injection script all behaved as documented. The closest thing to a bug is the "later channels silently skipped for a whole 15-minute tick" behavior above, which is a real, reproducible side effect but is arguably defensible (fail-fast, let cron retry) rather than wrong — it's a documentation gap, not a correctness defect:

```
$ python3 - <<'EOF'
... energy raises DeliveryError ...
EOF
raised: simulated failure
channels attempted this run: ['ops', 'energy']
configured channel order: ['ops', 'energy', 'everything']
```

`everything` — unaffected by `energy`'s failure — does not get its digest until the next cron invocation.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 7.5,
  "existing_code_respect": 4.5,
  "code_quality": 3.5,
  "documentation": 4,
  "total": 37,
  "would_merge": "yes-with-fixes",
  "one_line": "Excellent per-feed identity and receipts design; misses unbounded items-table growth."
}
```
