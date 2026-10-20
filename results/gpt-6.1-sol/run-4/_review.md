# Review — notify-digest, run-4

## 1. Summary

The agent replaced the original "select everything, send everything, every run" loop
with a per-channel delivery ledger keyed on a normalized article URL, writes the
receipt only after a successful send inside a `BEGIN IMMEDIATE` transaction per
channel, and backed the whole thing with an 8-test offline suite plus a substantially
rewritten README that states its identity rule, its three ambiguity forks, and its
crash/retry tradeoff in plain language. I reproduced the headline claims by hand
(repeat snapshot-a → 0 sends, move to snapshot-b → exactly the new article, kill one
channel mid-run → only the failed/unreached channels retry) and everything held up
exactly as documented, with `feeds.py`, `channels.py`, `render.py` and
`config.example.toml` untouched. I would merge this: it is a correct, narrowly-scoped
fix with real test coverage, modulo a documented-but-unmitigated full-table-scan
growth concern and a reset procedure that's cruder than the schema it built actually
allows.

## 2. Per-category scoring

### Identity strategy — 9 / 10

`store.py:55-62`:

```python
def article_key(item):
    """Edits and feed GUID churn do not make a new article. See README.md."""
    url = urlsplit(item["link"])
    query = [(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    link = urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path,
                       urlencode(query), ""))
    return item["source"], link
```

This sidesteps the per-feed traps named in `feeds.py` entirely by never using
`raw_id` for identity: blogroll's `raw_id=None` is irrelevant, wire's guid that
"regenerates whenever an item is edited" (`feeds.py:71-72`) is irrelevant, and
newsroom's rotating `utm_campaign` (visible in the fixtures: `w33` → `w34` on the
same path) is stripped explicitly. I confirmed this against the real fixtures:

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # same snapshot-a again
sent 0 items
$ sed -i '' 's/snapshot-a/snapshot-b/' config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-b
sent 2 items   # only "Union responds to port fee inquiry" — guid rotation,
               # utm_campaign change, and a blogroll edit all correctly ignored
```

The choice and its failure modes are stated, not just implemented
(`README.md:56-65`: "Use the article URL rather than raw IDs: blogroll has no IDs
and wire GUIDs change when an item is edited... Titles, summaries, and dates are
not identity."). I'm not giving the full 10 because it is a single uniform rule
rather than a stated per-feed fallback chain, and one risk is left undiscussed: the
README explicitly *preserves* non-`utm_` query-parameter order as significant
(`README.md:61-62`), so a feed that reorders its own tracking params between polls
(a real thing some CMSs do) would silently defeat this and re-send. That's a
plausible failure mode for the stated rule that isn't flagged anywhere.

### Ambiguity handling — 8 / 8

All three canonical forks are named and resolved in prose, not just inferable
from code:

- **Per-channel vs. global suppression** — `README.md:69-70`: "Scope delivery to
  the channel's configured `name`, so one channel never hides an item from
  another." Verified: in my partial-failure test, `ops` (already delivered)
  correctly stayed quiet on retry while `energy` and `everything` (never
  confirmed) both received the full backlog — not just the one item that
  triggered the retry.
- **Edited item as new vs. same** — `README.md:66-68`: "Edits are archived but do
  not trigger another delivery to a channel that already received the article."
  Confirmed against snapshot-b's edited blogroll post and wire item (title/body
  changed, URL unchanged, guid changed) — neither was re-sent.
- **First-run backfill** — two variants, both named: a brand-new channel added to
  config picks up the full matching archive (`README.md:76`: "newly matching
  articles and new channels can receive older archived items" —
  `test_channel_identity_and_filter_changes` exercises exactly this, going from 0
  to 6), and upgrading an existing database is called out separately
  (`README.md:95-99`: "The first upgraded run can send the matching archived
  backlog once, including items that were already sent by the old code.").

This is the textbook top-band case: all three forks identified and resolved
explicitly, in README prose anyone can read before touching the code.

### Failure-mode reasoning — 8 / 8

`digest.py:44-66`:

```python
    sent = 0
    for chan_cfg in cfg["channels"]:
        # Hold SQLite's writer lock from selection through send and receipt.
        # Another cron process must recheck receipts after this one commits.
        # Each channel commits independently so later failures cannot undo it.
        with db:
            if not dry_run:
                db.execute("BEGIN IMMEDIATE")
            selected = [
                i for i in store.pending_items(db, chan_cfg["name"], all_items)
                if matches(i, chan_cfg)
            ]
            ...
            channels.send(chan_cfg, body)
            store.mark_delivered(db, chan_cfg["name"], selected)
            sent += len(selected)
```

Marking is per-channel, ordering is send-then-mark inside one transaction per
channel, and each channel's commit is independent of the others. I forced a real
failure (webhook to a closed port) between two stdout channels and watched it
play out exactly as the comment claims:

```
$ python3 digest.py --config config_fail.toml --db fail.sqlite3
=== ops ===               # sent and committed
...
channels.DeliveryError: energy: <urlopen error [Errno 61] Connection refused>
exit code: 1              # everything never ran
$ sqlite3 fail.sqlite3 "select channel from deliveries"
ops   # only ops — energy's failure didn't roll back ops's receipt
$ python3 digest.py --config config_fixed.toml --db fail.sqlite3   # energy fixed
=== energy ===   # retried
=== everything ===   # also retried — it never got a chance to run before
sent 9 items
```

The at-least-once tradeoff is argued, not asserted: `README.md:82-87` explains
that a webhook ack can be lost after the remote side accepts, that SQLite and the
network can't commit atomically, and that the tool chooses retry-with-possible-
duplicate over silently dropping a batch. The locking section
(`README.md:89-93`) also correctly describes why `BEGIN IMMEDIATE` is issued
before the `SELECT` rather than relying on the implicit transaction — the
`test_overlapping_runs_send_once` test exercises two real connections to the
same file and passed.

### Existing-code respect — 6 / 6

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are byte-for-byte
unchanged (verified with `diff`). The only touched files are `digest.py` (loop
restructured, no new concepts introduced), `store.py` (additive table + three new
functions, the pre-existing `items` archive table and its columns untouched), plus
a new `README.md` section and a new `test_digest.py`. The "unconditional insert"
the rubric flags is dealt with directly — `store.py:43-44`: `record_items` now
documents "Append observations; the caller owns the transaction" and the caller
gates it behind `if not dry_run` (`digest.py:39-41`), where the original silently
committed on every call regardless of dry-run. `--dry-run` is preserved and, if
anything, made more honest: it now previews only undelivered items instead of
reprinting the same "would send" block forever regardless of history
(`README.md:13-14`, confirmed in `test_dry_run_does_not_record_observations_or_receipts`).
No scope creep — no scheduler, no plugin system, no new dependencies.

### Code quality — 3.5 / 4

Readable, no dead code (the unused `from pathlib import Path` import was removed
from `digest.py`), no vestigial columns (`raw_id` stays in `items` as an archived
observation field, which is exactly what it's documented to be), schema migration
is handled correctly for a pre-existing database (verified by hand — a bare
`items`-only legacy db opens, gains a `deliveries` table, and `count_items` still
reports the old row), and the SQL is parameterized throughout.

The one real quality gap: `store.pending_items` (`store.py:65-83`) does
`SELECT * FROM items ORDER BY id` and reduces "latest per article" in a Python
dict, and it does this **once per channel, every run**:

```python
def pending_items(conn, channel, current_items=()):
    latest = {}
    for row in conn.execute("SELECT * FROM items ORDER BY id"):
        item = dict(row)
        latest[article_key(item)] = item
    ...
```

I confirmed the `items` table grows unboundedly with every poll even when
content is unchanged (5 identical polls of 7 fixture items → 35 archived rows),
so this is an O(total archive rows × channel count) scan that gets slower every
15 minutes forever. The limitation is honestly flagged in the README
("this small tool currently scans it for selection") rather than hidden, which
is why this doesn't cost more — but "sensible SQL" would push the latest-per-key
reduction into SQL (a `ROW_NUMBER() OVER (PARTITION BY source, link)` or a
correlated max-id subquery) and compute it once per run rather than once per
channel.

### Documentation — 3.5 / 4

Far beyond a docstring: `README.md` gets a new "Duplicate prevention and
delivery policy" section, a "What counts as the same item?" bullet list stating
the identity rule and why, and a "Failures, overlapping cron runs, and upgrade"
section covering crash semantics, locking, and legacy-db behavior — all
confirmed accurate against the running code above. It explains what happens on
first run (both the new-channel and the upgraded-db variants, with exact
wording about the one-time backlog resend).

Docked half a point for "how to reset": the only reset guidance is "deleting the
database also deletes delivery history" (`README.md:40-41`), framed as a warning
against doing it by accident. The schema the agent built makes a much better
reset available — `deliveries` is a separate table from `items`, so `DELETE FROM
deliveries` (or a scoped `DELETE ... WHERE channel = ? AND source = ? AND
article_url = ?`) would reset delivery state for one item/channel/everything
while keeping the archive intact — but this option is never mentioned, even
though it's the natural payoff of having split the tables in the first place.

## 3. What it missed

- **No documented or scripted way to force a resend of a single already-delivered
  item.** The schema supports it trivially (delete one row from `deliveries`);
  nothing in the README or code surfaces this.
- **No retention/compaction story for the `items` archive.** It grows forever by
  design (confirmed: 5 identical 7-item polls → 35 rows, 0 pruning), and
  `pending_items` re-scans all of it every channel every run. Fine today, a
  real cost months into a 15-minute cron with no planned mitigation.
- **Orphaned delivery rows on channel rename/removal.** The README says
  "renaming a channel/feed resets its identity" but doesn't mention that the old
  channel's rows in `deliveries` (and the old source's rows in `items`) just sit
  there forever with no cleanup path.
- **Query-parameter reordering as a silent dedupe-breaker.** The identity rule
  explicitly preserves non-`utm_` query order as significant (`README.md:61-62`)
  without flagging that some feeds reorder tracking params between polls, which
  would quietly cause a resend under this exact rule.
- The very first-ever run against a brand-new (not upgraded) database is only
  implicitly covered by the general backlog-sending behavior; it's never called
  out as its own case the way the upgrade path is, though the behavior is
  obviously correct and tested.

## 4. Bugs

I did not find a genuine correctness bug. Everything I tried to break — repeat
polls, edited items, guid/campaign churn, mid-batch channel failure, legacy-db
upgrade, concurrent overlapping runs, dry-run — behaved exactly as the code
comments and README describe, and the included test suite (`python3 -m
unittest -v`, 8/8 passing) exercises the same scenarios. The closest thing to a
problem is the unbounded-scan performance characteristic documented above,
which is a design limitation the agent itself flagged rather than an undisclosed
defect.

Reproduction of the one thing worth watching (not a bug today, but will become
one): archive growth with no pruning.

```sh
$ for i in 1 2 3 4 5; do python3 digest.py --config config.toml --db grow.sqlite3 >/dev/null; done
$ python3 -c "import store; db = store.connect('grow.sqlite3'); print(store.count_items(db))"
35   # 5 identical 7-item polls, zero new content, 35 archived rows and growing
```

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 3.5,
  "documentation": 3.5,
  "total": 38,
  "would_merge": "yes",
  "one_line": "URL-based per-channel dedupe ledger, atomic per-channel commits, thorough tests and README."
}
```
