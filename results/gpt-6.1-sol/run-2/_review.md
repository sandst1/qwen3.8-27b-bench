# Review: notify-digest — repeat-notification fix

## 1. Summary

The agent added a `deliveries` ledger (`channel`, `source`, normalized `article_url`, composite primary key) and checks/records against it per channel inside a per-channel `BEGIN IMMEDIATE` transaction, leaving `feeds.py`, `channels.py`, `render.py`, and `config.example.toml` untouched. It identifies an item by `(source, normalized_link)` — stripping `utm_*`/`fbclid`/`gclid`, lowercasing scheme/host, sorting remaining query params — rather than by the feeds' own (absent/unstable) `raw_id`, and documents this choice, its three ambiguity forks, and its failure/retry semantics at length in the README, backed by a 10-test suite. I verified by hand against both fixture snapshots that re-polling the same snapshot sends nothing and moving to snapshot-b sends only the genuinely new item while suppressing the edited blogroll post, the edited wire item (regenerated guid), and the two newsroom items whose only change was a rotated `utm_campaign`. I would merge this as-is; it is one of the more complete and self-aware fixes this prompt is likely to produce.

## 2. Per-category scoring

### Identity strategy — 9 / 10

The identity function in `store.py:59-75`:

```python
def item_key(item):
    """Ignore tracking and edits, but preserve source and meaningful URL parts.

    All current providers have article permalinks; their IDs are absent or
    unstable. Keep this policy stable: changing it can resend existing items.
    """
    url = urlsplit(item["link"])
    query = sorted(
        (key, value)
        for key, value in parse_qsl(url.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"fbclid", "gclid"}
    )
    canonical = urlunsplit(
        (url.scheme.lower(), url.netloc.lower(), url.path, urlencode(query), "")
    )
    return item["source"], canonical
```

This deliberately discards `raw_id` for all three feeds and keys purely on `(source, normalized link)`. I ran it against both scenarios the fixtures are built to test:

- `newsroom`: entry `84121`'s URL carries `utm_campaign=w33` in snapshot-a and `utm_campaign=w34` in snapshot-b. Stripped of `utm_*`, both normalize to the same key, so re-polling snapshot-b with the same DB does **not** resend `84121`/`84118` — confirmed by direct run (only the brand-new `84130` "Union responds…" item appeared).
- `blogroll`: has no `raw_id` at all (`feeds.py:66`, `"raw_id": None`); the title/excerpt change between snapshots but the permalink doesn't, so it's correctly treated as the same item.
- `wire` (`generic` format): the `guid` is regenerated on edit (`wire-2026-08-14-0031` → `...-r2`), but the `link` doesn't change; keying on link instead of `raw_id` means the edited wire item is also correctly suppressed.

All three fixture traps named in the rubric are handled, and the README states the rationale and one explicit failure mode ("A genuinely changed permalink is a new identity," README:56). I'm not giving full marks because this is a single uniform policy, not a true per-feed/fallback chain: `newsroom`'s `entry_id` is in fact stable and never consulted, so a `newsroom` redesign that changes the URL path while keeping `entry_id` constant would silently cause a resend storm — a scenario within this strategy's blast radius that the README doesn't flag. The opposite failure mode (two genuinely different articles sharing a reused/redirect URL being wrongly merged) also goes unmentioned. These are foreseeable, not exotic, and a `raw_id`-first-with-link-fallback design would have closed them.

### Ambiguity handling — 8 / 8

All three forks named by the rubric are explicitly surfaced and resolved in the README, not just implemented silently:

- **Per-channel vs. global suppression** — "Each channel is independent: sending to `ops` never suppresses `energy`." (README:59), and the ledger's primary key `(channel, source, article_url)` (`store.py:30`) enforces it structurally.
- **Edited item as new vs. same** — "Titles, summaries, and publication dates are content, not identity: editing them must not trigger another notification." (README:50-51), which is exactly why `item_key` excludes those fields.
- **First-run backfill on an upgrade** — a dedicated "Existing databases" section (README:87-93): "Those rows prove only that an item was fetched, not which channels received it, so they cannot safely be backfilled as receipts. The first upgraded run sends the currently matching items once, then subsequent runs suppress them." This is tested directly (`test_existing_archive_is_preserved_but_not_treated_as_delivery`, `tests/test_digest.py:102-121`).

This is the top band verbatim: "All three forks identified and resolved explicitly."

### Failure-mode reasoning — 8 / 8

`digest.py:45-60`:

```python
for chan_cfg in cfg["channels"]:
    with store.delivery_transaction(db):
        selected = store.undelivered(
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
        store.record_deliveries(db, chan_cfg["name"], selected)
    sent += len(selected)
```

Marking is per channel, and the receipt (`record_deliveries`) is written strictly after `channels.send` succeeds, inside the same `BEGIN IMMEDIATE` transaction (`store.py:78-92`), so a crash between send and commit can only cause a *duplicate* send on retry, never a *lost* one. When channel 2 of 3 raises, the exception propagates (unchanged cron-retry contract from the original `channels.py` docstring), channel 3 is never attempted that run, but channel 1's receipt is already committed — verified by `test_failed_channel_retries_without_repeating_successes` (`tests/test_digest.py:55-66`), which shows the retry resends only to `energy` and `everything`, not `ops`.

The tradeoff is argued explicitly, not just implemented: "This is at-least-once delivery, not exactly-once... Recording before sending would instead silently lose messages on failure; we prefer retrying." (README:80-83). It goes further and reasons about overlapping cron invocations (a second poll starting before the first one finishes, a real risk at a 15-minute cadence if delivery is slow): the per-channel `BEGIN IMMEDIATE` serializes "selection AND sending" (`store.py:79-84`) so two processes can't both decide the same item is unsent, with the tradeoff of holding a write lock across an outbound webhook call stated outright: "Holding a write lock during delivery is a deliberate simple tradeoff for this small cron job." (README:77-78). `test_overlapping_runs_send_each_channel_once` (`tests/test_digest.py:137-178`) exercises this with two real threads and passes.

### Existing-code respect — 6 / 6

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are byte-for-byte unchanged (verified with `diff -rq` against the original). `--dry-run` is preserved and still skips both the send and the receipt write (`digest.py:54-57`), confirmed by `test_dry_run_does_not_consume_items`. The pre-existing `items` archive table is untouched — same columns, same unconditional insert — and the agent explicitly deals with the tension the rubric calls out rather than ignoring it: the docstring now reads "The `items` table archives observations, not delivery receipts. The separate `deliveries` ledger records successful sends..." (`store.py:1-5`), i.e. it recognized that repurposing an append-only observation log as a dedupe mechanism was the wrong move and added a purpose-built table instead of overloading the old one. Schema evolution is additive only (`CREATE TABLE IF NOT EXISTS deliveries ...`, `store.py:25-31`), so an existing production DB upgrades with zero migration risk — tested in `test_existing_archive_is_preserved_but_not_treated_as_delivery`.

### Code quality — 4 / 4

The diff is surgical: `digest.py` and `store.py` are the only production files touched, and the agent even removed a pre-existing unused `from pathlib import Path` import in `digest.py` while it was in there. `store.py` functions are short, each documented with *why*, not just *what* (e.g. `record_deliveries`'s docstring: "Called only after send succeeds, inside delivery_transaction."). The SQL is parameterized throughout, and the composite primary key on `deliveries` (`channel, source, article_url`) does double duty as the integrity constraint, not just an index. One minor nit not worth a deduction at this scale: `undelivered()` issues one `SELECT` per candidate item rather than a single batched query (`store.py:95-109`) — fine for the handful of items per poll this system handles, but would need revisiting if item volume grew.

### Documentation — 3.5 / 4

The README gained three substantial, accurate sections — "Why items are sent only once per channel," "Failures and overlapping cron runs," "Existing databases" (README:43-93) — plus a "Verification" pointer to the test suite. This is well above the "docstring alone" cap; it explains the dedupe mechanism, why URLs over IDs, per-channel independence, retry/at-least-once semantics, and upgrade behavior, and it's accurate against the code (the normalization description at README:52-53 matches `item_key` exactly). What's missing: no explicit operator-facing instruction for *resetting* dedupe state on purpose (e.g. "to force a resend to a channel, `DELETE FROM deliveries WHERE channel = ...`") — the closest the README gets is "Deleting it loses delivery history" (README:41), which describes a side effect, not a procedure. There's also no mention of the `deliveries` table growing unboundedly with no retention/pruning story. Docked half a point for the missing reset/retention guidance against an otherwise thorough writeup.

## 3. What it missed

- **Raw IDs are discarded everywhere, including where they're stable.** `newsroom`'s `entry_id` survives edits in the fixtures; the agent's policy never looks at it and relies entirely on URL stability. This happens to work against both fixture snapshots, but a backend URL restructuring (without an ID change) is a plausible failure this design doesn't anticipate or document, unlike the `blogroll`/`wire` cases it does call out.
- **No retention/reset tooling.** The `deliveries` table only grows; there's no documented way to prune it or to intentionally resend to a channel (e.g., after a filter misconfiguration caused bad items to be marked delivered). An operator's only lever is deleting the whole DB, which also destroys the archive.
- **Link-collision risk is one-sided in the documentation.** README:56 states a changed permalink creates a new identity (true), but never states the inverse: a *reused* URL (e.g., a redirect/landing page) for two genuinely different articles would be silently and permanently merged. Not exercised by the fixtures, but a logical consequence of the chosen design that goes unacknowledged.
- **No CLI knob for any of this.** Nothing like `--db` introspection, a `--mark-sent`/`--replay` escape hatch, or a startup log line stating how many pending/suppressed items exist. For an ops-run cron job that's a reasonable thing to skip for scope, but it means all state inspection has to happen through raw `sqlite3`.

## 4. Bugs

None found. I reproduced the core complaint and the fix end-to-end:

```
$ python3 digest.py --config config.toml --db run.sqlite3        # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db run.sqlite3        # snapshot-a again
sent 0 items
$ # switch config.toml to snapshot-b
$ python3 digest.py --config config.toml --db run.sqlite3        # snapshot-b
sent 2 items          # only the new "Union responds..." item, to ops + everything
```

The edited `wire` item (regenerated `guid`) and the edited `blogroll` post (new excerpt, same permalink) are correctly *not* resent on the snapshot-b run, matching the agent's stated design, and the 10-test suite (`tests/test_digest.py`) passes clean (`python3 -m unittest discover -s tests -v` → `Ran 10 tests ... OK`). I did not find a case in the fixtures or in adversarial manual testing (dry-run, feed failure, channel failure mid-run, overlapping threads, pre-existing legacy schema) where the implementation diverges from what the README promises.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 3.5,
  "total": 38.5,
  "would_merge": "yes",
  "one_line": "Per-channel ledger, normalized-URL identity, documented tradeoffs, full test suite; near-exemplary fix."
}
```
