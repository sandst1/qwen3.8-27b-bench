# Review — notify-digest, claude-opus-5-max, run-1

## 1. Summary

The agent replaced ad-hoc "did we ever see this" archiving with a proper per-channel delivery ledger (`sent` table keyed on `(channel, source, identity)`), computed identity per feed format in `feeds.py` specifically to survive each provider's known instability (rotating `utm_campaign`, in-place edits, regenerated `guid`), and marks delivery only after a channel send succeeds so a dead webhook retries instead of losing the batch. It backed this with 30 unit tests driven directly off `fixtures/snapshot-a` and `snapshot-b`, a schema migration for the existing production database, and a README section that states every non-obvious decision (per-channel ledger, edits-don't-renotify, at-least-once, `--mark-seen` for cutover) together with its rejected alternative. I ran it against both fixtures myself (below) and it behaves exactly as documented. I would merge this.

## 2. Per-category scoring

### Identity strategy — 10 / 10

Each feed format gets its own identity rule, chosen specifically against the trap the fixtures set up for it, in `feeds.py:13-49` (module docstring) and applied at `feeds.py:168-238`:

```python
# newsroom
"identity": _identity(
    "id", raw_id, name, r["headline"], r.get("published_at", "")
),
# entry_id survives edits and the weekly utm_campaign
# rotation on r["url"], so it is the trustworthy key here.

# blogroll
"identity": _identity(
    "url", normalise_url(r["permalink"]), name, r["title"], r.get("date", "")
),
# Permalink is all we have, and it is stable even when the
# post is retitled/re-edited -- which it is, routinely.

# generic/wire
"raw_id": r.get("guid"),   # kept for the archive, NOT used for identity
"identity": _identity(
    "url", normalise_url(r.get("link", "")), name, r["title"], r.get("pubDate", "")
),
```

I ran it against both fixtures to check this isn't just asserted:

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a again
sent 0 items
$ sed -i 's/snapshot-a/snapshot-b/' config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-b
sent 2 items    # only "Union responds to port fee inquiry" (genuinely new entry_id 84130)
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-b again
sent 0 items
```

All three traps are avoided in the actual run: the `utm_campaign` w33→w34 rotation on the two surviving newsroom entries did not resend (keyed on `entry_id`), the retitled/re-excerpted blogroll post did not resend (keyed on normalised permalink), and the wire item whose `guid` gained an `-r2` suffix did not resend (keyed on normalised link, guid ignored). `test_digest.py:121-157` (`TestProviderChurn`) asserts the same thing directly against the fixtures, one test per trap.

The identity-less fallback (`feeds.py:133-145`) also gets it right: rather than collapsing every item with no id and no link onto one key (which would silently eat real items), it hashes `source + title + published`, documented as "duplicate over drop, every time." `TestIdentityFallback` (`test_digest.py:319-330`) exercises this.

This is the 9-10 band verbatim: a fallback-chain identity, per format, that survives all three fixture traps, with the choice and its failure mode stated in the code next to it.

### Ambiguity handling — 8 / 8

All three named forks from the rubric are resolved explicitly, in code and in the README, not left to be inferred:

- **Per-channel vs global suppression** — `store.py:14-27` states the reasoning and the ledger's primary key is `(channel, source, identity)` (`store.py:64-70`). README repeats it under "Decisions worth knowing about" (README:113-118). Verified: `TestChannelIndependence` (`test_digest.py:160-191`) checks overlapping channels both get the item and that channel order in the config doesn't change the outcome.
- **Edited item as new vs same** — decided explicitly: identity never folds in title/summary (feeds.py:38-42, README:126-132), and the archive still updates the mutable fields on re-sight so the text stays queryable even though it doesn't re-notify (`store.record_items`, store.py:117-152; `TestProviderChurn.test_edits_are_recorded_in_the_archive_even_though_not_resent`, test_digest.py:149-157).
- **First-run backfill** — `--mark-seen` (digest.py:117-127, README:167-176) lets an operator adopt the current feed window without a notification burst on cutover, and is documented as the same tool for onboarding a new channel without back-filling it.

This is the top band: all three forks identified *and* resolved, with the resolution stated in both the code and the README rather than just happening to be correct.

### Failure-mode reasoning — 8 / 8

Marking is per-channel (`store.mark_sent(db, name, fresh)`, digest.py:107) and happens strictly after delivery:

```python
try:
    channels.send(chan_cfg, body)
except channels.DeliveryError as exc:
    print(f"warn: channel {name} failed: {exc}", file=sys.stderr)
    failures.append(name)
    continue          # not marked sent -> retried next tick

store.mark_sent(db, name, fresh)   # only reached on successful delivery
```

The at-least-once tradeoff is argued, not just implemented — `store.py:190-197` and README:120-124 both state it: a crash between POST and commit re-sends, which is deliberately preferred to a silent miss. One channel's `DeliveryError` doesn't block the others (digest.py:97-105), and I verified this against the test suite (`TestDeliveryFailures`, test_digest.py:194-228): a broken channel is retried on the next tick while a healthy one in the same run still gets delivered, and a broken *feed* doesn't stop the other two feeds from going out. This is the top band: per-channel marking, correct ordering, tradeoff argued.

### Existing-code respect — 6 / 6

`digest.py`'s control flow (`load_config` → `run_once` → `main`) and `render.py`/`channels.py` are untouched. `feeds.py` keeps its per-format branching and item shape, only adding an `identity` field next to the existing keys. The `items` archive is kept and its "unconditional insert" problem — the actual root cause of "same items over and over" being visible even in the archive — is fixed with an upsert (`store.py:117-152`) rather than replaced. `--dry-run` still works and writes no rows (`TestDryRun`, test_digest.py:231-241, and I confirmed `count_items`/`count_sent` stay 0 after a dry run). Schema migration for the box's existing database is handled in place (`store._migrate`, store.py:74-101) rather than assuming a fresh DB, which I exercised directly against a hand-built pre-migration schema and confirmed old rows survive with `identity IS NULL` and are excluded from the partial unique index rather than colliding.

One nit, noted under Bugs below: `--dry-run` still touches the filesystem (creates an empty schema file) even though the README says it "writes nothing to the database" — this doesn't affect any row data or dedup state, so it doesn't cost points here, but it's a slightly imprecise claim.

### Code quality — 4 / 4

`store.py`'s chunking in `unsent` (store.py:39-41, 155-187) guards against SQLite's default 999-host-parameter cap for large feeds — a real, non-hypothetical limit given the `(source, identity)` tuple membership test. SQL is sensible: `WITHOUT ROWID` on the ledger since it has no need of a surrogate key, a partial unique index so pre-migration NULL-identity rows don't collide with each other. No dead code or vestigial columns — `raw_id` is kept (for the archive, explicitly not for identity) rather than dropped, which is a defensible call, not leftover cruft. `WAL` mode is turned on with a stated reason (store.py:107-109).

### Documentation — 4 / 4

This clears the "docstring alone caps at 2" bar by a wide margin. README:44-186 covers: the redelivery mechanism end to end, a table of exactly which field churns on which feed and why the obvious key breaks (README:67-71), the per-channel ledger rationale, the at-least-once tradeoff, how edits are handled, how to reset/adopt state on an existing box (`--mark-seen`), first-run behavior for both the whole system and a newly added channel, and a "Known limits" section that flags the unbounded `sent` table growth and states the correct direction to fix it in if it ever matters. The docstrings in `feeds.py` and `store.py` also carry the same reasoning, so a reader in either place gets the full picture without needing the README.

## 3. What it missed

- **Unbounded ledger growth** is identified but not fixed (README:180-183, "Known limits"). Reasonable to leave for a system doing "a few items a day," and the direction for a future fix (reap only past the longest provider retention window, never shorter) is stated correctly.
- **Cross-provider deduplication** (the same story appearing in `newsroom` and `wire` as two different items) is explicitly out of scope and stated as such (store.py:23-26, README:137-140). Correct call for this task; flagged rather than silently ignored.
- **Concurrent cron ticks** (e.g. a slow run still executing when the next 15-minute tick fires) are not addressed anywhere — no lock, no discussion of what two overlapping `store.connect()` calls against the same SQLite file would do. Unlikely to bite in practice given WAL mode and short runs, but it's a genuine gap in the failure-mode story that isn't even named as a limitation.
- **`--dry-run` touches disk** — see Bugs.

## 4. Bugs

Nothing that loses or duplicates a notification. One cosmetic inaccuracy:

**`--dry-run` creates the SQLite file (with schema) even though nothing is written to it.** `main()` calls `store.connect(args.db)` unconditionally before checking `args.dry_run` (digest.py:134), and `connect()` always executes `SCHEMA` (store.py:104-114). Reproduction:

```
$ rm -f digest.sqlite3
$ python3 digest.py --config config.toml --db digest.sqlite3 --dry-run > /dev/null
$ ls -la digest.sqlite3
-rw-r--r--  1 topi  wheel  24576 ... digest.sqlite3      # file exists
$ sqlite3 digest.sqlite3 "SELECT COUNT(*) FROM items; SELECT COUNT(*) FROM sent;"
0
0
```

No rows are written and no dedup state is affected — `TestDryRun` correctly checks row counts, not file existence — so this is a documentation nit (README:15, "Writes nothing to the database") rather than a functional bug.

```json
{
  "identity_strategy": 10,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 4,
  "total": 40,
  "would_merge": "yes",
  "one_line": "Per-feed identity, per-channel ledger, at-least-once — all verified against both fixtures."
}
```
