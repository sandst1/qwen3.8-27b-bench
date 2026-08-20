# Review: claude-opus-5, run 4

## 1. Summary

The agent added a per-format `dedupe_key` (provider id for `newsroom`, tracking-stripped
link for `blogroll`/`generic`), a `deliveries` table keyed `(channel, source, dedupe_key)`
to suppress re-sends per channel rather than globally, a schema migration for existing
databases, and a README section that names the first-run, edit-vs-new, and per-channel
forks along with the crash/retry tradeoff. I ran it against both fixture snapshots
end-to-end (see evidence below) and every scenario the fixtures were built to exercise —
`utm_campaign` rotation, in-place edits, guid regeneration, migration of a pre-dedupe DB,
partial channel failure, dry-run — behaved exactly as documented. I would merge this.

## 2. Per-category scoring

### Identity strategy — 9/10

Per-format identity, stated with its failure modes in `feeds.py:9-27` and mirrored in
the README table. The three providers get three different treatments:

```python
# feeds.py, newsroom
"dedupe_key": "id:" + str(r["entry_id"]),
# entry_id is a stable primary key on the provider's side and
# survives retitles and edits. Deliberately NOT the URL: the
# utm_campaign on it rotates weekly...
```
```python
# feeds.py, _with_link_key (used for blogroll and generic)
def _with_link_key(item):
    link = normalise_link(item.get("link", ""))
    item["dedupe_key"] = "link:" + link if link else _content_key(item)
    return item
```

`normalise_link` (`feeds.py:47-77`) strips a documented set of tracking parameters,
lowercases host, drops default ports/trailing slash, but deliberately keeps non-tracking
query params and leaves the path case as-is. I verified this holds up:

- `newsroom` w33→w34 (`utm_campaign` changes on every item, including one whose
  `entry_id` and content are unchanged): correctly suppressed, confirmed by running
  snapshot-a then snapshot-b — only the genuinely new `entry_id: 84130` item appears.
- `blogroll`'s title/excerpt edit-in-place (`fixtures/snapshot-b/blogroll.json`,
  `"Notes on port fee arithmetic (updated)"`): correctly suppressed since the link is
  unchanged.
- `generic`'s guid regeneration (`wire-2026-08-14-0031` → `...-r2`): correctly
  suppressed by keying on link instead of guid.

Docked one point, not two: the content-hash fallback (`_content_key`, `feeds.py:79-89`)
keys on `title + published`, so an in-place edit to a titleless/linkless item would still
be treated as new — the code itself admits this ("acceptable only because the
alternative is no key at all") but it's an unaddressed edge no current feed hits, and a
non-tracking query-string identity choice like `?id=123` being kept (rather than always
stripped or hashed) is asserted but not fixture-tested.

### Ambiguity handling — 8/8

All three forks the rubric names are identified and resolved explicitly, in code and in
the README, not just in a docstring:

- **Per-channel vs. global suppression** — resolved and named:
  ```python
  # digest.py
  # The feeds hand us the same items on every 15-minute tick, so filter
  # out what this channel has already been sent. Per channel, not
  # globally: an item can belong in several digests.
  seen = store.already_delivered(db, name, selected)
  ```
  and in `store.py`'s module docstring: *"suppressing an item everywhere just because
  the 'ops' channel already got it would silently starve the 'everything' firehose."*
  I confirmed this directly: after channel `ops` fails, `energy` and `everything` still
  receive and record their items in the same run (see Bugs/verification below).

- **Edited item as new vs. same** — resolved and named, with the tradeoff stated:
  README: *"Edits do not re-notify... If a feed ever needs edits to re-notify, that has
  to be opt-in per feed, not the default."* Verified against both edited fixtures.

- **First-run backfill** — resolved and named: README: *"There is no delivery history
  for items sent before this existed, so the first run re-sends whatever is currently in
  the feed window. It settles from the second run on."* I confirmed the first run against
  snapshot-a sends all 6 items, and a second run against the same snapshot sends 0.

### Failure-mode reasoning — 8/8

Per-channel marking, correct ordering (send, then record only on success), and the
at-least-once tradeoff is explicitly argued rather than assumed:

```python
try:
    channels.send(chan_cfg, body)
except channels.DeliveryError as exc:
    # Don't record the delivery: the next tick retries this channel.
    # Keep going so one broken webhook doesn't starve the others.
    print(f"warn: channel {name} failed: {exc}", file=sys.stderr)
    continue

# Recorded only after a successful send. A crash in this gap re-sends
# the digest once on the next tick; recording first would instead lose
# it silently and forever. For a digest, a rare repeat beats a drop.
store.record_delivery(db, name, fresh)
```

I directly tested both halves of this:
1. Simulated `ops` raising `DeliveryError` mid-run: `energy` and `everything` still sent
   and recorded; only `ops` was left unrecorded (retried next tick). No other channel was
   starved.
2. Simulated a crash (`SystemExit`) between `channels.send` and `store.record_delivery`
   for `ops`: the `deliveries` table ended up with zero rows for that channel — the item
   would resend on the next tick rather than being silently dropped, exactly as the
   comment claims.

`channels.py` was also updated to make this contract explicit rather than propagating a
bare exception:
```python
# channels.py
`webhook` posts the digest body as JSON. `stdout` just prints it, which is
what we use locally. Delivery is best effort: a channel that is down raises
`DeliveryError`, and digest.py logs it, leaves the items unrecorded and moves
on to the next channel, so the next cron tick retries just that channel.
```

### Existing-code respect — 6/6

The `items` archive is kept as an archive (untouched semantics, `store.py` docstring:
*"Nothing else reads it"*), and its pre-existing unconditional-insert problem (every poll
re-inserting the same row, which the old schema had no way to prevent) is dealt with via
`INSERT OR IGNORE` plus a `UNIQUE(source, dedupe_key)` index, not by replacing the table.
A migration path handles existing on-disk databases from before dedupe existed
(`store.py:_migrate`, verified by hand-building a legacy 3-row-duplicate DB and confirming
it collapses to 1 row and gains a backfilled `dedupe_key` on first `connect()`).
`--dry-run` was preserved and verified to still (a) print without sending, and (b) not
write any `deliveries` rows — confirmed by running `--dry-run` twice back-to-back and
getting identical "4 item(s)" output both times, with `deliveries` at 0 rows and `items`
still archived. `feeds.py`'s and `channels.py`'s existing structure and comment style were
extended, not rewritten. No scope creep (no scheduler, no web UI, no new deps beyond
stdlib).

### Code quality — 4/4

Clean, no dead code. The `_MAX_KEYS_PER_QUERY` chunking in `already_delivered`
(`store.py:132-152`) is a bit of unrequested defensiveness (SQLite's default param cap
guards against a theoretical future problem the three small fixtures never approach) but
it's isolated, correct, and commented rather than left as a mystery constant:
```python
# Chunked because each key binds two parameters, and SQLite's
# parameter cap is 999 on older builds.
_MAX_KEYS_PER_QUERY = 400
```
No vestigial columns; `dedupe_key` replaces reliance on `raw_id` for identity while
keeping `raw_id` itself for the archive. SQL is straightforward parameterized
`INSERT OR IGNORE` / `DELETE ... WHERE sent_at < datetime('now', ?)`. `prune_deliveries`
runs after every real (non-dry-run) execution, keeping the table small without a separate
maintenance job.

### Documentation — 4/4

The README's "Deduplication" section (`README.md`) is not a docstring recap — it states
the same-field-fails-per-provider table, explains why "per channel not global," lists
first-run behavior, the 90-day retention caveat and *why* it must exceed the longest feed
window, the crash/retry tradeoff, and that links are normalized "for identity only" (the
rendered digest still shows the original tracked URL — I confirmed this: the sent digest
output above still shows `?utm_source=feed&utm_campaign=w34` in the body). It also warns
operators that `digest.sqlite3` is now load-bearing: *"if you delete or move
digest.sqlite3, the next run re-sends the current feed window."*

## 3. What it missed

- The content-hash fallback path (`_content_key`) is knowingly weak for edited items with
  neither link nor stable id — acknowledged in comments but not fixture-covered, since no
  current feed reaches it.
- No configuration knob to opt a specific feed into "edits re-notify" even though the
  README says that would need to be per-feed opt-in — it names the decision but doesn't
  build the extension point, which is a reasonable scope call but worth flagging.
- `already_delivered`/`record_delivery` do two round trips per channel per run (a select
  then an insert); fine at this scale, not discussed as a scaling concern beyond the
  param-cap chunking.
- The 90-day retention constant is a magic number with no config surface; acceptable
  given it's documented and named, but a future feed with a >90-day visibility window
  would need someone to remember to bump it — the README does warn about this explicitly,
  which mitigates the risk.

## 4. Bugs

None found. I reproduced the two fixture snapshots end-to-end and every documented
edge case (utm rotation, in-place edit, guid regeneration, migration, partial channel
failure, crash-before-record, dry-run non-mutation) and all matched the stated behavior.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 4,
  "total": 39,
  "would_merge": "yes",
  "one_line": "Per-feed identity, per-channel delivery ledger, argued failure semantics, verified against both fixtures."
}
```
