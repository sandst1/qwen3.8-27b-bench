# Review — notify-digest dedup fix

## 1. Summary

The agent added a per-channel `sent` ledger keyed on `(source, key, channel)`, chose a
per-feed-format stable identity (`entry_id` / permalink / link) instead of the unreliable
`raw_id`/guid, and marks items sent only after a successful delivery so a down channel is
retried without re-sending to channels that already got the batch. It also expires sent
markers after 30 days and rewrote the README to explain all of this. I ran it against both
fixture snapshots and a simulated partial-channel-failure and it behaved exactly as
documented; I would merge this.

## 2. Per-category scoring

### Identity strategy — 9.5 / 10

`feeds.py` picks a different stable field per format instead of reusing `raw_id`, and each
choice cites the failure mode it's dodging:

```python
# feeds.py:59-63 (newsroom)
"raw_id": str(r["entry_id"]),
# entry_id is stable across re-fetches. The URL is not: the
# provider appends rotating utm_campaign tracking params, so
# the link must not be used as the identity.
"key": str(r["entry_id"]),
```
```python
# feeds.py:84-96 (generic/wire)
# "generic": has a guid, but the provider regenerates it whenever an
# item is edited (typos, added tags, retitles). The link is stable
# across edits, so it is the identity.
...
"key": r["link"],
```

I verified this against the fixtures rather than trusting the comments. Snapshot-b rotates
`utm_campaign` on two newsroom items, regenerates the wire `guid` (`wire-2026-08-14-0031` →
`...-r2`) and retitles a blogroll post. Running snapshot-a then snapshot-b:

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ sed -i s/snapshot-a/snapshot-b/ config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-b
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```
Only the genuinely new newsroom entry (`entry_id 84130`) was delivered; the rotated-campaign,
regenerated-guid, and retitled items were all correctly suppressed. This is the top band: a
fallback-chain-by-format identity that survives all three feeds' specific failure modes, and
the choice is stated inline. Docked half a point only because `blogroll`'s permalink-as-key
is asserted but not defended against the one thing that *could* break it (a CMS that changes
permalinks on retitle) — a minor, forgivable gap given the fixtures never exercise it.

### Ambiguity handling — 6 / 8

Two of the three canonical forks are named and resolved explicitly, the third is decided
silently (correctly).

- **Per-channel vs global**, named in code and README:
  ```python
  # digest.py:47-49
  # Skip what this channel has already received (the `sent` table in
  # store.py). Dedup is per channel, not global: a channel that never
  # matched an item before can still receive it if it starts matching.
  ```
  Verified: adding a webhook channel with new keywords on a re-run of an already-seen batch
  still delivers to it, because the PK is `(source, key, channel)`, not `(source, key)`.

- **Edited item as new vs same**, named in `feeds.py` (see quotes above) and in the README's
  "Dedup" section: "The provider regenerates the `guid` whenever an item is edited, so edited
  items are still recognised as the same item." Verified above with the wire fixture.

- **First-run backfill** is not named anywhere. On an empty `sent` table every current feed
  item is "new" and gets sent in full on the very first cron tick — a defensible default, and
  I confirmed it behaves that way, but neither the code comments nor the README says "first
  run will deliver everything currently in the feeds" or considers whether that's desirable
  for a 15-minute cron job bootstrapping against a feed with months of backlog.

Two forks resolved and stated, one resolved silently but correctly — solidly mid-band.

### Failure-mode reasoning — 7.5 / 8

Marking happens per channel, strictly after the send call, and the reasoning is written down
in two places:

```python
# digest.py:62-67
channels.send(chan_cfg, body)
# Mark only after a successful send: if a channel is down the
# exception propagates (see channels.py) and the next cron tick
# retries it, without re-sending to channels that already got it.
# A dry run marks nothing, so it never consumes items.
store.mark_sent(db, chan_cfg["name"], selected)
```

I did not take this on faith. I built a 3-channel config where the middle channel
(`energy`, type `webhook`) points at a closed port:

```
$ python3 digest.py --config config_fail.toml --db digest.sqlite3
=== ops ===             # ran and was marked
...
channels.DeliveryError: energy: <urlopen error [Errno 61] Connection refused>
$ echo $?
1
```
`ops` (before the failure) was marked sent; `everything` (after the failure) never ran this
tick because the unhandled `DeliveryError` crashes the whole process — this is inherited from
`channels.py`'s pre-existing "let it propagate, cron retries" design, not something the agent
introduced or worsened. On the next tick, `ops` was correctly *not* re-sent, `energy`
succeeded once the URL was fixed, and `everything` finally got its full un-sent batch — no
duplicates, no drops. The README states the guarantee explicitly ("Items are marked after a
successful send... without re-sending to channels that already got the items"), which is the
argued at-least-once choice the rubric asks for. Not full marks because the crash-on-first-
failure ordering (rather than continuing to later channels and marking a partial failure list)
is inherited rather than actively chosen/discussed by the agent — it works, but the interaction
between the pre-existing crash design and the new per-channel ledger isn't spelled out.

### Existing-code respect — 6 / 6

No rewrites. `feeds.py` gained one new dict key (`key`) per return branch; the three format
branches and their `raw_id` computation are untouched. `store.py` keeps the `items` archive
insert unconditional and untouched, and additively creates a `sent` table — `CREATE TABLE IF
NOT EXISTS` means an existing `digest.sqlite3` migrates in place with no manual steps. `--dry-
run` was checked and still works (prints, marks nothing, confirmed by running it twice in a
row with no state change). No scope creep — no plugin system, no scheduler, no new CLI flags.

### Code quality — 4 / 4

`sent_keys`/`mark_sent`/`prune_sent` are short, single-purpose, and the SQL is sensible
(composite primary key doing double duty as the uniqueness constraint, `INSERT OR IGNORE` to
make marking idempotent, a plain `datetime('now', '-N days')` prune). No vestigial columns:
`raw_id`/`items` archive is untouched from the original and was already documented as
intentionally kept. No dead code introduced.

### Documentation — 4 / 4

The README gained a "Dedup" section that names the identity chosen per feed format and *why*,
states dedup is per-channel not global, gives the retention constant and what happens when a
marker expires, and explains the mark-after-send ordering and `--dry-run` interaction. This
goes well beyond a docstring and covers reset behavior implicitly (delete `digest.sqlite3` or
wait out `SENT_RETENTION_DAYS`) — though it doesn't literally spell out "to reset dedup, delete
the sqlite file," which would have been a nice explicit line to add.

## 3. What it missed

- **First-run backfill** is not discussed. A fresh `digest.sqlite3` will deliver the entire
  current contents of every feed on the first cron tick — for feeds with a lot of backlog this
  could be a noisy first digest. The agent picked a default and moved on without naming it as a
  choice.
- **`items` table growth** is untouched (as before) — it's an unbounded archive with no
  retention, which is unchanged from the original's documented intent, so not a regression, but
  also not reconsidered even though the agent was already touching `store.py`'s schema.
- **The crash-on-channel-failure ordering** (an exception on channel N aborts channels N+1..).
  is inherited, not actively evaluated against the alternative of catching and continuing so
  later channels still get their (independent) markers this tick.

## 4. Bugs

None found. I ran:
- snapshot-a twice back to back → second run `sent 0 items` (correct suppression).
- snapshot-a then snapshot-b → only the single genuinely-new newsroom item delivered; rotated
  `utm_campaign`, regenerated wire `guid`, and edited blogroll title were all correctly treated
  as "already sent."
- `--dry-run` twice in a row → no state change, no items consumed.
- A 3-channel run with the middle channel forced to fail → first channel marked and not
  re-sent on retry, failed channel retried and eventually succeeded, last channel (never
  reached before) received its full batch with no duplication to the first two channels.

All matched the documented behavior; nothing broke.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 7.5,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 4,
  "total": 37,
  "would_merge": "yes",
  "one_line": "Per-feed identity + per-channel sent ledger, verified against fixtures; well documented."
}
```
