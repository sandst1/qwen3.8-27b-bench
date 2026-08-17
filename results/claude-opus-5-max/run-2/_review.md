# Review: notify-digest dedupe fix

## 1. Summary

The agent replaced "poll and send whatever matches" with a per-feed identity
scheme (`feeds.py`), a per-channel `deliveries` ledger written only after send
succeeds (`store.py`, `digest.py`), and a `--seed` flag plus schema migration
for existing deployments; it also added a 24-case regression test suite and
rewrote the README to explain all of it. I ran the test suite (24/24 pass) and
replayed both fixture snapshots by hand — identical polls send nothing,
snapshot-b sends exactly the one genuinely-new article, and the utm-rotated,
guid-regenerated, and edited-in-place items are all correctly suppressed. I
would merge this as-is; it is close to a model answer for the prompt given.

## 2. Per-category scoring

### Identity strategy — 10/10

The agent read the fixtures, tabulated what mutates per feed, and picked a
different stable field per format instead of one global rule:

```python
# feeds.py:40-47
newsroom  entry_id. The provider's own identifier, stable while the url
          churns. Preferred over the normalised link because it does not
          depend on _is_tracking() keeping up with whatever parameter they
          add next.
blogroll  normalised permalink. There is no id at all, and title/summary are
          edited in place.
generic   normalised link. The guid is regenerated on every edit, so it is
          actively harmful here despite looking like the obvious choice.
```

And a fallback chain for the one feed where the primary field can be absent:

```python
# feeds.py:174-182
"key": (
    _key(name, f"entry-{entry_id}")
    if entry_id is not None
    else _link_key(name, r["url"], r["headline"])
),
```

I verified this against both fixture pairs by hand:

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # same snapshot again
sent 0 items
$ # switch config to snapshot-b
$ python3 digest.py --config config.toml --db digest.sqlite3
sent 2 items    # only "Union responds to port fee inquiry" (genuinely new),
                # matching ops + everything
```

None of the utm-rotated newsroom item, the guid-regenerated wire item, or the
edited-in-place blogroll item reappeared. `_link_key` also falls back to a
title hash rather than dropping the item outright if a link is ever missing
(`feeds.py:134-139`), and the failure mode of that fallback (a retitle slips
through as "new") is stated, not silently accepted.

### Ambiguity handling — 8/8

All three named forks in the rubric are explicitly resolved in code and prose:

- **Per-channel vs. global suppression** — resolved and justified:
  ```python
  # store.py:15-18
  Why the ledger is keyed per channel rather than globally: an item can match
  several channels, and in the default config it does — the port fee story goes
  to both "ops" and "everything". A single global "sent" flag would let whichever
  channel is processed first consume the item and starve the rest.
  ```
  Verified by test and by inspection: `unsent()` queries `deliveries WHERE
  channel = ?`, and `TestPerChannelLedger.test_a_new_channel_gets_the_backlog`
  confirms a newly-added channel still gets the full backlog while existing
  channels stay quiet.

- **Edited item as new vs. same** — decided explicitly per feed (blogroll and
  generic both treat in-place edits as the *same* item; see the identity table
  above), with the tradeoff of that choice named for the one path where it can
  go wrong (title-hash fallback, `feeds.py:134-139`).

- **First-run backfill** — given its own flag, not left to run silently one
  way or the other:
  ```python
  # digest.py:64-70
  if seed:
      # Catching up an existing deployment: mark as delivered without
      # sending, so the backlog already sitting in the feeds is not
      # announced all over again.
      store.mark_sent(db, name, fresh)
  ```
  and the README states the alternative explicitly: "On a brand new install
  you can skip `--seed` if you want the current contents of the feeds as a
  first digest."

### Failure-mode reasoning — 8/8

Marking is per-channel and ordered after the send, with the at-least-once
tradeoff stated rather than assumed:

```python
# digest.py:80-90
try:
    channels.send(chan_cfg, body)
except channels.DeliveryError as exc:
    print(f"warn: channel {name} failed: {exc}", file=sys.stderr)
    continue          # not marked sent -> retried next tick

store.mark_sent(db, name, fresh)
```

```python
# digest.py:39-42 (docstring)
Delivery is at-least-once: the ledger is written *after* channels.send()
returns, so a channel that is down repeats its digest on the next tick
rather than silently swallowing it. Marking first would turn an outage into
"items vanish", which is far harder to notice than a duplicate.
```

Tested directly (`TestDeliveryFailures`): a failing "ops" channel doesn't
consume its items, doesn't block "energy"/"everything" from being marked sent,
and the withheld items are delivered on the very next tick once the channel
recovers.

### Existing-code respect — 5.5/6

`--dry-run` still works and is tested to have zero side effects on
deliveries. The `items` archive is kept, and the previous bug in it (a new
row per sighting, so `first_seen` was really "last seen") is fixed with a
unique partial index and `ON CONFLICT ... DO NOTHING`, not a rewrite of the
table's purpose:

```sql
-- store.py:43-44
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_key
    ON items(key) WHERE key IS NOT NULL;
```

A migration path for pre-existing databases is included and tested
(`TestMigration`). `channels.py` is barely touched (docstring only).
`feeds.py` triples in size (83 → 219 lines), which is a large surface change
for a "fix the duplicate bug" prompt, but the growth is identity logic the
task requires, not scope creep — no plugin system, no new delivery type, no
unrelated refactor. The half-point deduction is for one undocumented
side effect I found by hand: `--dry-run` still writes to the `items` archive
table (see Bugs below), which is a small crack in the "dry run changes
nothing" claim the README and docstring both make.

### Code quality — 4/4

Clean, no dead code, no vestigial columns (`raw_id` is kept but is now
explicitly archival, not used for identity — stated in `feeds.py`'s
docstring). SQL is sensible (`ON CONFLICT ... DO NOTHING`, a partial unique
index scoped correctly with a matching `WHERE` on the insert). The migration
is minimal and correct.

### Documentation — 4/4

The README has a dedicated "Not sending the same thing twice" section
covering the per-feed identity table, the per-channel-ledger decision, the
at-least-once tradeoff, and an "Upgrading an existing box" section describing
`--seed`. This clears the "docstring alone caps at 2" bar comfortably — the
same reasoning is duplicated, appropriately, in both the code comments and the
README.

## 3. What it missed

- **`--dry-run` still writes to the `items` archive.** Confirmed by hand:

  ```
  $ python3 -c "... digest.run_once(cfg, db, dry_run=True) ..."
  items archived after dry run: 2
  deliveries after dry run: 0
  ```

  The docstring's claim ("a dry run must not change what a subsequent real
  run would deliver") is true for `deliveries`, the table that actually
  drives dedupe, but the archive does get non-idempotent-looking new rows.
  Harmless in practice (the unique key means a real run afterwards just hits
  `ON CONFLICT DO NOTHING` and `first_seen` is preserved), but it's an
  inconsistency between the stated intent and the code that a future reader
  could trip over.
- `run_once(dry_run=True, seed=True)` is only rejected at the CLI layer
  (`ap.error` in `main()`); calling `run_once` directly with both flags set
  silently prefers `seed` and ignores `dry_run`. Not exercised by any test,
  and not documented as a precondition on the function itself.
- The tracking-parameter list (`_TRACKING_PARAMS`/`_TRACKING_PREFIXES`) is a
  fixed allowlist of known ad-tracking params. The comment is honest about
  this being a deliberate, narrow choice, but it means a fourth feed that
  rotates some other non-utm parameter would need a code change — reasonable,
  but the "three feeds" framing of the whole fix means this will need
  revisiting the day a fourth feed shows up with a new churn pattern.

## 4. Bugs

None that break correctness. The only behavioral surprise found by testing is
the dry-run archive write described above, which does not affect delivery
correctness (verified: a real run after a dry run still delivers exactly the
un-marked items, `TestDryRunAndSeed.test_dry_run_does_not_consume_items`
passes and I reproduced it independently). I did not find any case in the
fixtures, or any constructed variant of them, where an item was duplicated or
dropped incorrectly.

```json
{
  "identity_strategy": 10,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 5.5,
  "code_quality": 4,
  "documentation": 4,
  "total": 39.5,
  "would_merge": "yes",
  "one_line": "Near-model dedupe fix: per-feed identity, per-channel ledger, migration, tests, docs."
}
```
