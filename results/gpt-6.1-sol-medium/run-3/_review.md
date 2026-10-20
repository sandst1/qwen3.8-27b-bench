# Review: notify-digest, run-3 (gpt-6.1-sol)

## 1. Summary

The agent diagnosed the real bug correctly — `digest.py` never consulted any
delivery history, so every item was resent on every 15-minute tick — and fixed
it with a new `deliveries` ledger keyed on `(channel, source, canonical link)`,
written inside a per-channel SQLite transaction committed right after
`channels.send` succeeds. It left `feeds.py`, `channels.py`, and `render.py`
untouched, preserved `--dry-run`'s no-op contract, added a 9-case regression
suite that actually exercises both fixture snapshots and a simulated
overlapping cron run, and wrote several README paragraphs naming its
ambiguity and failure-mode choices instead of burying them in a docstring. I
would merge this: it is correct against both fixture snapshots, it does not
break anything that worked before, and the next person to touch it has
somewhere to look.

## 2. Per-category scoring

### Identity strategy — 9/10

`store.py:58-71`:

```python
def item_key(item):
    """Use the source and permalink, ignoring UTM tracking and fragments.

    Raw IDs can be absent or regenerated; titles and summaries can be edited.
    Keep other query parameters: they may distinguish different articles.
    Different sources remain distinct even if they link to the same article.
    """
    parts = urlsplit(item["link"])
    query = urlencode([
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ])
    link = urlunsplit(parts._replace(query=query, fragment=""))
    return item["source"], link
```

I ran the actual fixtures to check this rather than just read it:

```
$ python3 digest.py --config config.example.toml --db digest.sqlite3      # snapshot-a
sent 12 items
$ python3 digest.py --config config.example.toml --db digest.sqlite3      # snapshot-a again
sent 0 items
$ python3 digest.py --config cfg_b.toml --db digest.sqlite3               # switched to snapshot-b
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
=== everything ===
Firehose — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
sent 2 items
$ python3 digest.py --config cfg_b.toml --db digest.sqlite3               # snapshot-b again
sent 0 items
```

That is the correct answer: out of 12 items across both snapshots, exactly one
is genuinely new (newsroom's `entry_id` 84130), and it is the only thing
resent. All three documented feed quirks are handled:

- **wire**'s `guid` regenerates on edit (`wire-2026-08-14-0031` →
  `wire-2026-08-14-0031-r2` in `fixtures/snapshot-b/wire.json`) — doesn't
  matter, because `raw_id` plays no role in identity at all.
- **blogroll** has no `raw_id` (`feeds.py:66`, `"raw_id": None`) and the
  snapshot-b post is retitled "(updated)" with a changed excerpt — same
  `permalink`, so it is correctly treated as the same item and not resent.
- **newsroom**'s links carry `utm_campcampaign=w33` → `utm_campaign=w34`
  between snapshots — stripped by `item_key`, so the unedited items aren't
  misidentified as new.

The README states the choice and its blind spots plainly (`README.md:51-58`,
`54-58`):

> Identity uses the permalink because blogroll has no IDs and wire
> regenerates IDs on edits... A changed permalink counts as new; URL aliases
> and redirects are not resolved.

This clears the bar for the top band, but it's not quite a true fallback
chain: it is one uniform rule (link, source) applied to every feed, and it
silently discards `raw_id` even where it is the *more* reliable signal.
`newsroom`'s `entry_id` is never shown to be unstable anywhere in `feeds.py`
or the fixtures — only `wire`'s "generic" format is documented as
guid-regenerating (`feeds.py:71-72`). If newsroom ever changes a slug on
retitle while keeping `entry_id` stable (plausible, untested), this identity
scheme would wrongly treat the edit as a new item and resend it, where a
`raw_id`-first fallback would not. The agent never surfaces that this
tradeoff exists for newsroom specifically — it only discusses blogroll and
wire. That gap keeps this out of a 10.

### Ambiguity handling — 7/8

Two of the three canonical forks are named and resolved explicitly, in
prose, not just in behavior.

**Per-channel vs global suppression** (`README.md:59-61`):

> Duplicate identities in one poll are sent once per channel... An item
> delivered to `ops` can still be delivered to `energy` if it matches there
> later.

Verified with `tests/test_digest.py:81-90`
(`test_channel_history_is_independent_of_other_channels_and_filters`), which
adds a second channel after the first has already consumed an item and
confirms the new channel still gets it. This is the right call — global
suppression would silently drop items from channels that haven't seen them
yet, which is the classic wrong default the rubric calls out.

**Edited item as new vs same** (`README.md:51-53`):

> Titles, summaries, publication dates, and raw IDs are not part of
> identity: edits do not trigger another notification.

This is a real decision with a real cost (the "(updated)" blogroll post never
gets re-announced even though the content materially changed — a corrected
figure), and the agent says so rather than letting it fall out by accident.

**First-run backfill** is addressed, but only glancingly, under the upgrade
scenario (`README.md:82-89`, "Existing databases"):

> The first upgraded run sends the currently matching items once; subsequent
> runs suppress them.

This is the right behavior and it generalizes to a from-scratch deployment
(empty ledger ⇒ everything currently matching goes out once), but the README
frames it entirely as a migration note for databases that already have an
`items` archive. A fresh `python3 digest.py` with no `--db` file at all will
flood every channel with every currently-matching item on its first tick —
true, reasonable, and never stated as "this is what happens on day one,"
which is the literal fork the rubric names.

### Failure-mode reasoning — 8/8

`digest.py:45-63`:

```python
for chan_cfg in cfg["channels"]:
    # Serialize selection + send + ledger update across overlapping cron
    # runs. Commit each channel so a later failure cannot undo its success.
    with db:
        db.execute("BEGIN IMMEDIATE")
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

This is per-channel marking (each channel gets its own transaction), correct
ordering for an at-least-once guarantee (the ledger write happens strictly
after `channels.send` returns, inside the same transaction, so a channel that
raises never gets credited), and I verified the "channel two fails after
channel one succeeded" case directly:

```
test_partial_failure_preserves_success_and_retries_failed_channel ... ok
```

(`tests/test_digest.py:54-67`) — `ops` succeeds, `energy` raises mid-run
(aborting the whole tick, since nothing catches the exception — same
best-effort design `channels.py`'s existing docstring already specified), and
on the next call only `energy` and `everything` send; `ops` is correctly
skipped because its items are already in the ledger.

The tradeoff is argued, not just implemented (`README.md:75-80`):

> This is retry-oriented delivery, not exactly-once delivery: if the receiver
> accepts a webhook but the response is lost, or the process dies after
> sending but before committing, the next run can resend it. Marking items
> before sending would instead risk silently losing notifications.

and the concurrency cost of the chosen locking strategy is disclosed rather
than hidden (`README.md:69-73`):

> SQLite `BEGIN IMMEDIATE` covers selection, sending, and recording.
> Overlapping processes sharing the database wait for the writer (up to 60
> seconds)... This deliberately holds a write lock during delivery to keep
> this small job's concurrency model simple.

That's an honest admission that a slow webhook call holds a write lock on the
whole database for the length of its timeout — a real cost, stated plainly.
This is the fullest treatment the rubric asks for at the top band.

### Existing-code respect — 6/6

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are
untouched (confirmed by diff — no hunks against any of them). The only
`digest.py` changes are the per-channel transaction wrap and the removal of
an already-unused `from pathlib import Path` import. `store.record_items`
(the "unconditional insert" the rubric specifically flags) is left exactly as
it was — the agent didn't try to make the archive double as the dedupe
mechanism, it added a separate `deliveries` table instead, and says why
(`README.md:46-47`, `87-89`):

> Previously, every poll appended to `items` and sent every matching item
> again; the archive was never consulted and did not track delivery.
> ...Archive rows continue to represent observations (including repeated
> polls), not unique articles or confirmed deliveries.

`--dry-run` was checked, not just assumed: it still reports what would be
sent based on the ledger, never writes to the ledger, and reprints nothing
once a real run has caught up (verified live — see "Bugs" section note on
how this was checked).

### Code quality — 3.5/4

`store.py` is small, documented with reasoning rather than restatement, and
the schema migration for a pre-existing database is handled with a plain
`CREATE TABLE IF NOT EXISTS` plus a dedicated test
(`test_existing_archive_is_preserved_but_not_assumed_delivered`,
`tests/test_digest.py:130-148`) that seeds a *pre-ledger* legacy schema and
confirms the old `items` rows survive untouched while the new ledger starts
empty. No vestigial columns were added; `raw_id` keeps doing its original
archival job and plays no role in the new ledger.

The one blemish is `store.py:74-93`'s `undelivered()`:

```python
def undelivered(conn, channel, items):
    selected = []
    seen = set()
    for item in items:
        key = item_key(item)
        if key in seen:
            continue
        seen.add(key)
        delivered = conn.execute(
            "SELECT 1 FROM deliveries WHERE channel = ? AND source = ? AND link = ?",
            (channel, *key),
        ).fetchone()
        if delivered is None:
            selected.append(item)
    return selected
```

One `SELECT` per candidate item instead of a single batched query. Harmless
at the scale of this job (a handful of items every 15 minutes) but it's the
kind of thing a reviewer would flag and ask to be batched if the feed list
ever grew.

### Documentation — 3.5/4

This clears the "docstring alone" floor by a wide margin — the README gained
a dedicated "Duplicate prevention and delivery semantics" section plus an
"Existing databases" subsection (`README.md:44-90`) that covers identity,
all three ambiguity forks (with the caveat above on how crisply the
first-run one is named), how to reset history (`README.md:40-42`: "Deleting
or changing the database resets delivery history"), and the retry-vs-loss
tradeoff. It also added a `## Verification` section pointing at the new test
suite. Docked half a point for the same reason ambiguity handling lost a
point: "what happens on first run" is answered only in the migration-path
framing, not stated as a fact about a brand-new deployment.

## 3. What it missed

- **Newsroom's `raw_id` is strictly more reliable than the link it discards
  it for**, and the README never discusses this. The chosen identity (link
  only) happens to work for all three fixtures, but it is a single rule, not
  a per-feed fallback chain, and the tradeoff of dropping a stable ID in
  favor of a URL that *could* change on edit (as blogroll's did) is never
  named for newsroom specifically — only for the two feeds where discarding
  `raw_id` is unambiguously the right call.
- **True first-run behavior** (brand-new `--db`, no prior ledger at all) is
  never stated as its own fact; it's only described as a side effect of the
  "upgrading an existing database" scenario. An operator reading the README
  top-to-bottom would have to infer that a fresh deployment floods every
  channel with everything currently in the feeds on its first tick.
- **No bulk/batched lookup** in `store.undelivered` — a per-item `SELECT` is
  a design smell the agent didn't flag or justify, unlike the write-lock
  tradeoff it did call out.
- **No mention of what happens if an item disappears and reappears** in a
  feed (e.g., pagination window). The agent discusses the adjacent case
  (newly added channel + already-vanished item — `README.md:62-65`) but not
  this one.
- Case normalization of the link itself (scheme/host case) is not addressed;
  `item_key` would treat `HTTPS://X.test/a` and `https://x.test/a` as
  different identities. Not exercised by the fixtures, not mentioned.

## 4. Bugs

I did not find anything outright broken. Specifically checked and confirmed
working:

- Full regression suite passes: `python3 -m unittest discover -s tests -v`
  → 9/9 OK.
- Manual run against `snapshot-a` then `snapshot-b` (reproduction above in
  §2) sends exactly the one genuinely-new item and nothing else, on both the
  first pass and a repeat pass.
- `--dry-run` never writes to the ledger: running dry, then running for
  real, then running dry again shows an empty preview on the third
  invocation (nothing left unsent) — confirms dry-run both doesn't mark
  things delivered and doesn't resend things a real run already delivered.
- Partial-failure retry (`ops` succeeds, `energy` raises) does not re-send
  `ops` on the next tick and does not lose `energy`'s items — verified via
  `test_partial_failure_preserves_success_and_retries_failed_channel`.
- Mixing `BEGIN IMMEDIATE` with Python's default `sqlite3` isolation level
  does not throw "cannot start a transaction within a transaction" — this is
  a plausible footgun with manual `BEGIN` statements in Python's `sqlite3`
  module, and it works correctly here (verified via
  `test_overlapping_runs_send_once` actually running two real threads against
  one database file, not a mock).

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 7,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 3.5,
  "documentation": 3.5,
  "total": 37,
  "would_merge": "yes",
  "one_line": "Correct per-channel dedupe ledger, verified against both fixtures, well-documented, minor gaps only."
}
```
