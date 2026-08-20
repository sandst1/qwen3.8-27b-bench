# Review: notify-digest dedup fix (claude-opus-5, run-1)

## 1. Summary

The agent correctly diagnosed that the codebase never persisted "have we sent
this" at all — it only kept a write-only archive — and built a proper
per-channel delivery ledger keyed on a per-provider identity function that
explicitly defeats each feed's specific quirk (rotating `utm_campaign`,
in-place edits with no id, and a regenerated `guid`). It added a schema
migration for the live database, a `--seed` flag for onboarding new channels,
per-channel failure isolation with a non-zero exit code, and a 14-test suite
plus a substantially rewritten README that documents every non-obvious
decision. I would merge this; it is very close to what I would want a
senior engineer to hand back for this exact bug report, modulo one small
`README` inaccuracy noted below.

## 2. Per-category scoring

### Identity strategy — 10/10

`feeds.py` builds a per-format key rather than trusting one field globally:

```python
def item_key(source, raw_id, link, title, trust_raw_id):
    if trust_raw_id and raw_id:
        return f"{source}:id:{raw_id}"
    normalised = normalise_link(link)
    if normalised:
        return f"{source}:link:{normalised}"
    digest = hashlib.sha256((title or "").encode()).hexdigest()[:16]
    return f"{source}:title:{digest}"
```

and per format decides whether the provider's id is trustworthy:

```python
# newsroom: entry_id is a real item id and survives edits
return _with_keys(items, name, trust_raw_id=True)
...
# blogroll: no id at all, titles/excerpts edited in place
return _with_keys(items, name, trust_raw_id=False)
...
# generic (wire): guid is a revision id, not an item id — ignore it
return _with_keys(items, name, trust_raw_id=False)
```

`normalise_link` strips rotating tracking params (`utm_campaign` etc.) and
the fragment before a link is used as identity — the specific thing that
defeats "just dedupe by link" for the `newsroom` feed.

I verified this against the fixtures rather than trusting the docstring. Running
`snapshot-a` then `snapshot-b` against the same db:

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a again
sent 0 items
$ # switch config to snapshot-b, same db
$ python3 digest.py --config config.toml --db digest.sqlite3
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```

Only the genuinely new newsroom item (`entry_id 84130`) is delivered; the
`utm_campaign`-rotated newsroom item, the wire item whose `guid` was bumped to
`-r2`, and the blogroll post retitled `"... (updated)"` are all correctly
suppressed. The included `test_digest.py` pins exactly these three cases
(`test_rotating_utm_parameter_is_not_a_new_item`,
`test_edited_title_and_excerpt_is_not_a_new_item`,
`test_regenerated_guid_is_not_a_new_item`) plus
`test_only_one_item_is_genuinely_new_in_snapshot_b`, and all 14 tests pass.
Failure modes are stated, not just implied — the README is explicit that a
genuine correction to an item will *not* be re-sent, and calls that an
accepted trade for a digest (as opposed to an alert).

### Ambiguity handling — 8/8

All three forks named in the rubric are identified and resolved explicitly,
in both code comments and the README, not just picked silently:

- **Per-channel vs. global suppression** — `store.py`:
  ```
  ## Why deliveries is keyed per channel
  A given item can match several channels. If we recorded delivery globally,
  the first channel to receive an item would suppress it for every other
  channel, and those channels would silently never see it. So the ledger is
  keyed (channel, item_key) — each channel gets its own independent view.
  ```
  and the schema backs it: `PRIMARY KEY (channel, item_key)` in `deliveries`.
  Verified with `test_each_channel_is_tracked_independently`.

- **Edited item as new vs. same** — `feeds.py` docstring: "Title and summary
  are never part of the identity... The flip side is that a genuine
  correction will not be re-sent to a channel that already saw the original —
  that is the intended trade, since these are digests, not alerts."

- **First-run backfill** — handled with an explicit `--seed` flag rather than
  silently swallowing the initial backlog or silently sending it:
  ```python
  ap.add_argument("--seed", action="store_true",
                  help="mark everything currently in the feeds as already "
                       "delivered, without sending it. Use when adding a "
                       "channel so it starts from now instead of receiving "
                       "the entire backlog.")
  ```
  and the migration path for the *existing* deployed database is separately
  reasoned about in the README ("the first run after upgrading sends one
  more full digest... Use `--seed` for that first run if you would rather
  nobody gets it").

### Failure-mode reasoning — 8/8

`digest.py:run_once` marks delivery **after** a successful send, per channel,
and isolates one channel's failure from the others:

```python
try:
    channels.send(chan_cfg, render.digest(selected, chan_cfg))
except channels.DeliveryError as exc:
    print(f"warn: channel {name} failed: {exc}", file=sys.stderr)
    failed = True
    continue

# Only after a successful send. See store.py on at-least-once.
store.mark_delivered(db, name, keys)
```

The at-least-once vs. at-most-once tradeoff is argued explicitly, not just
implemented, in `store.py`:

```
* mark-then-send would lose items permanently whenever a webhook is down,
  because the ledger would claim they were delivered.
* send-then-mark can, if we die between the two, resend one digest.
That makes delivery at-least-once. For a digest, an occasional repeat after a
crash is a much cheaper failure than an item nobody ever sees.
```

`main()` returns non-zero on a channel failure so cron surfaces it rather
than silently logging into a file nobody reads. Verified with
`test_failed_delivery_is_retried_and_does_not_block_other_channels`: a
`nonsense-type` channel fails, `failed=True`, later channels still get sent,
and the failed channel's items are *not* marked delivered so a subsequent run
picks them up.

### Existing-code respect — 6/6

`render.py` and `channels.py` are untouched (`diff` against the original is
empty for both). `feeds.py`'s per-format branching structure is kept and
extended (`_with_keys` wraps the existing per-format list comprehensions
rather than restructuring them). The archive table (`items`) is kept for its
stated purpose ("did this ever come through?") and its unconditional-insert
problem is fixed directly (`INSERT OR IGNORE` plus a new `UNIQUE INDEX
idx_items_key`), rather than being ripped out. `--dry-run` still behaves
correctly — I confirmed manually that a `--dry-run` invocation records zero
rows in `deliveries` and does not suppress the following real run:

```
$ python3 digest.py --config config.toml --db digest.sqlite3 --dry-run
$ python3 -c "import store; db=store.connect('digest.sqlite3'); print(store.count_deliveries(db))"
0
```

A schema migration (`_migrate`) is included for the box's live database
rather than assuming a fresh start, with the tradeoffs of the migration
(legacy archive rows won't match new keys, so a handful of one-off duplicate
archive rows appear post-upgrade) called out and accepted in a comment rather
than silently left as a surprise.

### Code quality — 4/4

`delivered_keys` chunks its `IN (...)` query at 500 placeholders to stay
under SQLite's variable limit — a real detail, not cargo-culted:

```python
for start in range(0, len(keys), 500):
    chunk = keys[start:start + 500]
    placeholders = ",".join("?" * len(chunk))
    rows = conn.execute(
        "SELECT item_key FROM deliveries"
        f" WHERE channel = ? AND item_key IN ({placeholders})",
        [channel, *chunk],
    )
```

No dead code, no vestigial columns (`raw_id` is kept in `items` deliberately,
for the archive's stated purpose, and is separate from the new `item_key`
identity column). The migration is a genuine, tested schema evolution
(`test_migration_collapses_a_pre_dedup_database`), not a `DROP TABLE`.

### Documentation — 4/3.5

The README gains a substantial "Not resending things" section covering the
identity table per provider, why the ledger is per-channel, the
at-least-once argument, `--seed` semantics, and what happens to an existing
deployed database on upgrade — this is not merely a docstring, it's the kind
of write-up the task explicitly asked for ("leave the codebase in a state
where the next person to touch it understands what you chose and why").

One inaccuracy costs half a point: the README says

```
`--dry-run` prints what would be sent instead of sending it, and records
nothing — a dry run never suppresses a later real send.
```

"Records nothing" is not quite true — `run_once` unconditionally calls
`store.record_items(db, feed_cfg["name"], items)` for every feed *before* the
per-channel loop, so a `--dry-run` invocation does write rows into the
`items` archive table (confirmed above: 6 archive rows after a `--dry-run`
run against an empty db). The claim that matters for correctness —
`deliveries` stays empty, so a later real run is unaffected — is true and
is what the test (`test_dry_run_records_nothing`) checks, but the README's
wording overstates it in a way that could mislead an operator who expects
`--dry-run` to touch the database not at all.

## 3. What it missed

- The `--dry-run`/archive interaction above: a minor, silent decision (dry
  runs still populate `items`) that the README states incorrectly rather
  than either fixing or naming accurately.
- No handling for the case where a provider's `raw_id` itself collides across
  genuinely distinct items (not tested in the fixtures, and not claimed to be
  handled) — a reasonable scope boundary, but not called out as an
  assumption the way the other tradeoffs are.
- The migration's `GROUP BY source, link` collapse for legacy rows assumes
  `link` was already a decent within-source identity for old data; if a
  provider had reused the same link for edited-and-republished content
  pre-migration, that collapse would under-count history in the archive. This
  is a low-stakes corner (the archive is stated to be write-only/informational)
  and is explicitly flagged as an accepted approximation in the `_migrate`
  docstring, but it's still an assumption about data the agent never saw.
- Nothing is said about clock skew or `published` field trustworthiness for
  sort order — out of scope for this bug, but also not mentioned as
  out-of-scope.

## 4. Bugs

None found that affect correctness of the stated goal (stopping duplicate
sends). Everything I tried to break — immediate re-run, cross-snapshot run on
the same db, a simulated pre-migration database, a failing channel
interleaved with working ones, `--dry-run` non-suppression, `--seed` — behaved
as documented and as the test suite asserts. The only issue found is the
README wording inaccuracy above (documentation category), not a functional
bug: `deliveries` — the table that actually gates sending — is genuinely
untouched by `--dry-run`.

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
  "one_line": "Per-format identity chain, per-channel ledger, migration, tests; one README overstatement."
}
```
