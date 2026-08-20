# Review — claude-opus-5, run-3

## 1. Summary

The agent introduced a `feeds.identity()` function that canonicalises a link (lowercased scheme/host, no fragment, no trailing slash, tracking params like `utm_*`/`gclid` stripped) as the dedup key, added a per-channel `sent` ledger in `store.py`, and rewired `digest.run_once` to send-then-mark so a delivery failure doesn't lose items. It also wrote a schema migration for pre-existing databases, a 15-case test suite, and a README section that walks through why each obvious identity choice (guid, content hash, raw link) fails on one of the three fixture feeds. I would merge this: I ran the actual fixtures (snapshot-a then snapshot-b) and it delivers exactly the one genuinely-new item and nothing else, and all 15 tests pass unmodified.

## 2. Per-category scoring

### Identity strategy — 10/10

`feeds.py:48-98` picks the canonicalised link, with an explicit fallback for link-less items, and states the failure mode for each of the three feeds in the docstring:

```python
def identity(item):
    """Return the stable dedup key for a normalised item.
    ...
      * `raw_id` (guid) is unusable. `blogroll` has no id at all, and `wire`
        regenerates its guid whenever an item is edited
        (`wire-...-0031` becomes `wire-...-0031-r2` when a comment is added).
      * title/summary hashing is unusable. `blogroll` retitles in place ...
      * the raw link is unusable. `newsroom` stamps a fresh `utm_campaign`
        onto every url each week (`w33` -> `w34`) ...
    """
    url = (item.get("link") or "").strip()
    if not url:
        return f"{item['source']}\x00{item.get('raw_id') or item.get('title', '')}"
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in _TRACKING_PARAMS]
    ...
```

I verified this against the actual fixtures, not just the agent's own tests. `wire` really does regenerate its guid (`wire-2026-08-14-0031` → `wire-2026-08-14-0031-r2`, same link) between snapshot-a and snapshot-b; `newsroom` really does add a new entry (84130) alongside the two carried-over ones. Running `digest.py` against snapshot-a then swapping the config to snapshot-b sent exactly the new item, twice (once to `ops`, once to `everything`), matching the docstring's claim precisely. The one stated blind spot — two genuinely distinct items sharing a URL — is named as a tradeoff rather than ignored, with an escape hatch ("give that feed its own identity rule here").

### Ambiguity handling — 8/8

All three named forks from the rubric are explicitly surfaced, not just implemented:

- **Per-channel vs global suppression** — `store.py:12-15`: "The ledger is keyed on (channel, identity), not on identity alone... a global 'have we sent this?' would let whichever channel ran first swallow the item and starve the others." Backed by `test_overlapping_channels_each_get_their_copy`.
- **Edited item as new vs same** — this *is* the identity docstring's whole argument (blogroll retitles, wire re-guids); the choice (same item) is stated and defended.
- **First-run backfill** — README.md:93-95: "First run on an empty database sends everything currently in the feeds... If that matters for a new channel, seed the ledger before enabling delivery."

### Failure-mode reasoning — 8/8

Per-channel marking, correct ordering, and the tradeoff is argued in three places (code, channels.py docstring, README). `digest.py:66-79`:

```python
try:
    channels.send(chan_cfg, body)
except channels.DeliveryError as exc:
    # Leave the ledger alone so these items are retried on the next
    # tick, and keep going: one broken webhook should not stop the
    # other channels from getting their digest.
    print(f"warn: channel {name} failed: {exc}", file=sys.stderr)
    continue

# Record only after the channel accepted it. The reverse order would
# turn any delivery failure into permanently dropped items, and a
# missed item is worse than a repeated one.
store.mark_sent(db, name, fresh)
```

This gives at-least-once semantics and says so. `test_failed_delivery_is_retried_not_dropped` and `test_one_broken_channel_does_not_block_the_others` both pass, and I re-ran them independently.

### Existing-code respect — 6/6

`feeds.py`'s three `_normalise` format branches (newsroom/blogroll/generic) are untouched byte-for-byte; the agent only wrapped them in `fetch()` to attach `identity`. `render.py` is untouched (`diff` confirms). `--dry-run` still works and, per `digest.py:59-64`, deliberately skips `mark_sent` — verified: a dry run followed by a real run still sends items. The archive's unconditional-insert problem (flagged as fine "for now" in the original docstring) is dealt with via `INSERT OR IGNORE` plus a unique index (`store.py:94-118`), rather than left as future work.

### Code quality — 4/4

`sent` uses `WITHOUT ROWID` with a `(channel, identity)` primary key, which is the right shape for a pure lookup table. The migration (`store.py:58-91`) backfills the new column, collapses historical duplicates keeping the earliest `first_seen`, and is idempotent (tested in `MigrationTests`). No dead code or vestigial columns; `raw_id` is kept for archive/debugging purposes, not used in the hot path.

### Documentation — 3.5/4

The README's "Deduplication" section explains the identity choice with a comparison table, the per-channel ledger rationale, and the send-then-mark ordering, and "Operational notes" covers migration and first-run behavior. It is missing one item the rubric asks for explicitly: **how to force a resend / reset dedup state** (e.g. "delete rows from `sent`, or wipe the db"). Nothing in the README or code tells the next operator how to do this if they need to re-notify a channel.

## 3. What it missed

- **No documented reset procedure.** If someone needs to force a channel to re-receive an item (e.g., after a bad digest render), there's no `DELETE FROM sent WHERE channel = ?` recipe anywhere.
- **Concurrent cron overlap** isn't discussed — if a run takes longer than 15 minutes cron could invoke a second overlapping process against the same sqlite file. Not required by the prompt, but a genuine failure mode of "every 15 minutes from cron" that goes unaddressed.
- **The `sent` table's unbounded growth is flagged but not solved** — the README explicitly punts on this ("do not add pruning... without checking the feed window first"), which is a reasonable and honestly-stated non-decision rather than a silent one.
- A stray `config.toml` (a real, filled-in config generated from `config.example.toml`) is left in the delivered directory; it's gitignored but shouldn't really be part of the handoff artifact. Cosmetic, not scored.

## 4. Bugs

None found. I independently reproduced the two key claims rather than trusting the agent's own tests:

```
$ python3 digest.py --config config.toml --db digest.sqlite3     # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3     # same again
sent 0 items
$ python3 digest.py --config config-b.toml --db digest.sqlite3   # snapshot-b
=== ops ===
* Union responds to port fee inquiry  [newsroom]
=== everything ===
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```

This matches the fixture diff exactly (`newsroom` entry 84130 is the only new id between snapshots; `wire`'s guid rotates from `wire-2026-08-14-0031` to `-r2` with the same link and is correctly treated as the same item). All 15 tests in `test_digest.py` pass unmodified in a fresh copy.

```json
{
  "identity_strategy": 10,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 3.5,
  "total": 39.5,
  "would_merge": "yes",
  "one_line": "Canonical-link identity, per-channel ledger, send-then-mark; well tested and documented."
}
```
