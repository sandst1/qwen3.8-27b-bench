# Review

## 1. Summary

The agent replaced the original "archive everything, then send everything matching every run" loop with a per-channel delivery ledger (`deliveries` table) keyed by `(channel, source, canonical-link)`, taking the link rather than the provider's unstable `raw_id`/`guid` as item identity, and wrapped each channel's select-send-mark sequence in a `BEGIN IMMEDIATE` transaction to stop overlapping cron ticks from double-sending. It left `feeds.py`, `channels.py`, `render.py` and the config format untouched, wrote a 10-test regression suite that exercises both fixture snapshots plus partial-failure and concurrency scenarios, and documented every non-obvious decision (identity, per-channel vs. global suppression, edited-item handling, first-run backfill, retry guarantee) in the README. I verified independently by running `digest.py` against snapshot-a then snapshot-b with a fresh `/tmp` copy: the second run sent only the one genuinely new item, not the three edited ones. I would merge this.

## 2. Per-category scoring

### Identity strategy — 9.5 / 10

The identity function is a single, uniform strategy (not a per-feed fallback chain) that works because it sidesteps every unstable field the three feeds use, rather than special-casing them:

```python
# store.py:59-73
def item_key(item):
    """Use permalinks, not mutable content or missing/regenerated provider IDs.

    Strip only known tracking parameters and fragments; meaningful query values
    and path case must continue to distinguish different articles.
    """
    url = urlsplit(item["link"])
    query = sorted(
        (key, value)
        for key, value in parse_qsl(url.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"fbclid", "gclid"}
    )
    return urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path,
                       urlencode(query), ""))
```

I ran it against the actual fixtures to confirm it against the three documented failure modes:

- `newsroom`: rotates `utm_campaign` (`w33`→`w34`) on every item, every run. Stripped by the `utm_` check — confirmed, "Regulator opens inquiry into port fees" (same `entry_id`, new `utm_campaign`) was **not** resent on snapshot-b.
- `wire` ("generic" format): regenerates `guid` on edit (`wire-2026-08-14-0031` → `wire-2026-08-14-0031-r2`), link unchanged. `raw_id` is never consulted for identity, so this is correctly treated as the same item — confirmed not resent.
- `blogroll`: `raw_id` is `None` for every item (`feeds.py:66`). Link-based identity doesn't care — confirmed "Notes on port fee arithmetic (updated)" was not resent despite title and excerpt changing.

Manual repro (scratch copy, not this directory):

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a again
sent 0 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-b
=== ops === Ops digest — 1 item(s): Union responds to port fee inquiry [newsroom]
=== everything === Firehose — 1 item(s): Union responds to port fee inquiry [newsroom]
sent 2 items
```
Only the genuinely new `port-fees-union` article was sent; the three edited items were correctly suppressed.

The failure mode is stated, not just implied: README.md:56-60 says "If a provider changes its actual permalink, it is treated as new; revisit identity rules with tests if that becomes a real provider behavior," and the tracking-param strip list (`utm_*`, `fbclid`, `gclid`) is an explicit, acknowledged allowlist rather than a general heuristic — a future tracking parameter (e.g. `ref`, `mc_cid`) would defeat it, and the agent says so rather than pretending otherwise. Docking half a point because the allowlist is hardcoded and the three fixtures don't actually stress-test a *missing* link, only a missing `raw_id` — but the reasoning is sound and explicit, which is what the top band asks for.

### Ambiguity handling — 8 / 8

All three forks named in the rubric are explicitly identified and resolved in prose, not just encoded silently:

- **Per-channel vs. global suppression** — chosen explicitly: the ledger's primary key is `(channel, source, item_key)` (store.py:24-30), and README.md:48 states "A separate `deliveries` table now records successful sends, keyed by **channel name, source name, and canonical permalink**." A new/renamed channel gets a full backlog (`README.md:65`), which is the correct, stated consequence of per-channel scoping.
- **Edited item as new vs. same** — chosen explicitly: "Edits to an already delivered article do not trigger another send" (README.md:53), because identity is link-only.
- **First-run / upgrade backfill** — chosen explicitly and argued, not defaulted into: "We deliberately do not backfill delivery history from the archive: 'fetched' does not mean 'delivered'... The first real run after upgrading therefore sends all currently matching items once" (README.md:92-95).

This is the textbook "all three forks identified and resolved explicitly" case the 7-8 band describes.

### Failure-mode reasoning — 8 / 8

```python
# digest.py:44-65
sent = 0
for chan_cfg in cfg["channels"]:
    with db:
        if not dry_run:
            # Lock BEFORE checking the ledger, through send and commit, so
            # overlapping cron processes cannot both select the same items.
            db.execute("BEGIN IMMEDIATE")
        selected = store.pending_items(
            db, chan_cfg["name"],
            [i for i in all_items if matches(i, chan_cfg)],
        )
        if not selected:
            continue
        body = render.digest(selected, chan_cfg)
        if dry_run:
            print(f"--- would send to {chan_cfg['name']} ---")
            print(body)
            continue
        channels.send(chan_cfg, body)
        store.record_delivery(db, chan_cfg["name"], selected)
    sent += len(selected)
```

Marking is per-channel (one `BEGIN IMMEDIATE`/commit per channel, inside the loop), and the ledger write happens strictly after `channels.send` returns without raising — the correct ordering for at-least-once. If channel 2 raises, channel 1's commit already happened (separate `with db:` scope from a prior loop iteration) and channel 2/3's transaction rolls back, so the next cron tick retries exactly the channels that didn't finish. I exercised this directly via the agent's own test (which I ran, not just read):

```
test_successful_channels_survive_later_failure ... ok
```
— energy fails, ops still gets marked delivered, and the retry run resends only energy + everything, not ops. The at-least-once vs. exactly-once tradeoff is argued, not asserted: README.md:81-87 explicitly calls out the ambiguous-failure window ("a webhook might accept a request and then time out, or the process might die after sending but before committing... SQLite cannot atomically commit with an external webhook; exactly-once would require receiver-side idempotency support"). It also correctly reasons about the *second* real failure mode implied by "every 15 minutes" — overlapping cron invocations — with the `BEGIN IMMEDIATE` lock and a `timeout=60` connect argument sized against the channel webhook's 10s timeout (store.py:34-36), and that's covered by a real threaded test (`test_overlapping_process_connections_send_only_once`) that I ran and confirmed passes.

### Existing-code respect — 6 / 6

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are **byte-for-byte unchanged** (verified by `diff`). `digest.py` and `store.py` are edited, not rewritten — the diff is additive (new `deliveries` table, new `pending_items`/`record_delivery`/`item_key` functions, locking added around the existing loop body) rather than a restructure. The rubric's specific concern — "reusing the `items` archive is fine *if* the unconditional insert is dealt with" — is addressed head-on by *not* using the archive for identity at all: store.py:1-5's docstring says "An observation is not proof of delivery. Existing archive rows therefore never seed the ledger," and `record_items` (store.py, unchanged) keeps doing its unconditional insert exactly as before, now understood as pure archival rather than (mis)used as a delivery marker. `--dry-run` was verified intact: it previews pending items without calling `channels.send` or `record_delivery`, and a subsequent dry run after a real send correctly shows nothing pending (confirmed via `test_dry_run_previews_pending_without_consuming`, which I ran, and by a manual dry-run/real-run pair).

### Code quality — 3.5 / 4

Clean, no dead code — the agent even removed an unused `from pathlib import Path` import from `digest.py` that was dead in the original. Schema migration is handled correctly and simply: `CREATE TABLE IF NOT EXISTS deliveries (...)` is additive, leaves the existing `items` table and its rows alone, and was verified against a pre-existing database in `test_old_archive_is_not_delivery_history` (passed when I ran it). SQL is parameterized throughout.

Two minor deductions:
- `pending_items` (store.py:76-91) issues one `SELECT` per candidate item rather than a single batched query. Harmless at this scale (a handful of items per 15-minute poll) but not the most "sensible SQL" under load.
- The control flow in `digest.py:44-65` relies on `continue` inside a `with db:` block to skip `sent += len(selected)` for both the "nothing selected" and "dry run" cases. It's correct (confirmed by output: dry run reports "sent 0 items" even though 12 items were previewed) and it's commented, but a reader has to notice that `continue` exits the context manager cleanly *and* skips the trailing accumulator line — a short helper function would have been more obviously correct than relying on loop-control subtlety.

### Documentation — 4 / 4

This clears the "docstring alone caps at 2" bar by a wide margin. The README gained a dedicated "## Deduplication decisions" section (README.md:44-68) explaining identity choice and its scope, a "### Success, failure, and overlapping cron runs" section (README.md:70-87) explaining per-channel transactional marking and arguing at-least-once explicitly, and a "### Existing databases / rollout" section (README.md:89-96) explaining exactly what happens on the first run after upgrading and why backfill was rejected. It also documents how to reset delivery state, if bluntly: "Deleting the database loses delivery history and causes current feed items to be sent again" (README.md:42) — this is a correct statement of the reset mechanism (delete the db) even though it doesn't offer a scalpel (e.g. `DELETE FROM deliveries WHERE channel=...` to reset one channel only).

## 3. What it missed

- **Feed-rename resets history, silently.** The README calls out that renaming a *channel* resets its delivery history (README.md:64-68), but says nothing about renaming a *feed*. Since `source` (the feed's `name` in config) is part of the ledger key (`store.py:81`, `store.py:97`), renaming `wire` to `newswire` in `config.toml` would cause every item from that feed to look "new" again and be resent to every channel, exactly mirroring the channel-rename case that *is* documented. This is the same class of decision the agent handled well for channels but didn't extend to feeds.
- **No retention/pruning story.** Both `items` and `deliveries` grow forever (one row per item per run for `items`, one row per channel/item for `deliveries`). Over months of 15-minute polling this is an unbounded, if slow, leak. Not mentioned anywhere — not necessarily wrong for a first pass, but it's the kind of operational note the README's otherwise-thorough "rollout" section could have carried.
- **Tracking-parameter allowlist is a strip-list, not a strip-pattern.** `utm_*`, `fbclid`, `gclid` are hardcoded (store.py:69-70). The agent is honest about this being a deliberate, narrow choice (README.md:54-56), but it is still a decision an operator will eventually have to revisit by hand, and there's no obvious extension point (e.g. a config-level allowlist) offered.
- **No discussion of the same real-world story appearing in two feeds.** This is explicitly scoped out ("two feeds covering the same story are independent," README.md:61) rather than missed outright — flagging it here only because it's a plausible next source of "why did I get this twice" complaints, and the README states the position but not the reasoning for it.

## 4. Bugs

I did not find anything outright broken. Specifically checked and found sound:
- Dry-run does not consume the pending-items ledger and does not print anything on a second consecutive dry run after a real send (manually reproduced).
- Transaction rollback on a mid-run channel failure does not lose or duplicate the channels that already succeeded (reproduced via the agent's `test_successful_channels_survive_later_failure`, which I ran rather than took on faith).
- Two overlapping `digest.py` processes against the same db file do not double-send (reproduced via the agent's threaded `test_overlapping_process_connections_send_only_once`, which I ran).
- Old databases (pre-upgrade, no `deliveries` table) upgrade cleanly without losing `items` rows (reproduced via `test_old_archive_is_not_delivery_history`).
- Full regression suite (`python3 -m unittest discover -s tests -v`) passes 10/10 in a fresh `/tmp` copy, and `pyflakes` reports no unused names across all five modules.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 3.5,
  "documentation": 4,
  "total": 39,
  "would_merge": "yes",
  "one_line": "Correct per-channel ledger, link-based identity, honest tradeoffs, well tested and documented."
}
```
