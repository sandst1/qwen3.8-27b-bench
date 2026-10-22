# Review: notify-digest dedupe fix

## 1. Summary

The agent diagnosed the actual cause (the per-run loop re-sent every matching item on every tick because nothing ever tracked delivery), introduced a `deliveries` ledger keyed on `(channel, source, canonical-permalink)`, and wrapped each channel's select→send→mark-sent sequence in a `BEGIN IMMEDIATE` transaction so overlapping cron invocations can't double-send. It touched only `digest.py`, `store.py`, and `README.md` — `feeds.py`, `channels.py`, `render.py`, and the config format are untouched, and `--dry-run` still works. I verified the dedupe logic against both fixture snapshots, a simulated partial-channel-failure, a simulated feed outage, a real webhook receiver, and two real concurrent OS processes sharing one SQLite file, and it behaved correctly in all of them; I would merge this as-is.

## 2. Per-category scoring

### Identity strategy — 10/10

`store.py:59-73`:
```python
def item_key(item):
    """Use permalinks, not mutable content or missing/regenerated provider IDs. ..."""
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
Identity is `(source, item_key(link))`, not `raw_id`. This is the correct call given the fixtures: `newsroom`'s `raw_id` (`entry_id`) is actually stable, but its URL rotates `utm_campaign`; `blogroll` has no `raw_id` at all; `wire`'s `guid` is explicitly regenerated on edit (`wire-2026-08-14-0031` → `wire-2026-08-14-0031-r2`) while its link doesn't change. A `raw_id`-first strategy would have broken on `wire`. I ran both snapshots for real:

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a again
sent 0 items
$ # point config at snapshot-b, run again
sent 2 items   # only "Union responds to port fee inquiry" (a genuinely new entry_id/URL)
```
The edited `wire` item (guid regenerated) and the edited `blogroll` post (no id, changed excerpt) were correctly *not* resent. The failure mode is stated, not just implied — README.md:51-60: *"If a provider changes its actual permalink, it is treated as new; revisit identity rules with tests if that becomes a real provider behavior."* That is exactly the top-band bar: a fallback-chain-equivalent identity that survives rotating `utm_campaign`, missing `raw_id`, and a regenerated `guid`, with the one remaining failure mode named.

### Ambiguity handling — 8/8

All three named forks from the rubric are resolved explicitly in prose, and I confirmed each one behaviorally:

- **Per-channel vs global suppression** — resolved per-channel. `store.py:24-30` keys `deliveries` on `(channel, source, item_key)`; README.md:48-49: *"A separate `deliveries` table now records successful sends, keyed by **channel name, source name, and canonical permalink**."*
- **Edited item as new vs same** — resolved as same. README.md:53: *"Edits to an already delivered article do not trigger another send."* Verified above with the `wire` and `blogroll` edits.
- **First-run backfill** — resolved as "don't backfill, send everything once." README.md:91-96 (`Existing databases / rollout`): *"We deliberately do not backfill delivery history from the archive... The first real run after upgrading therefore sends all currently matching items once."* I verified this directly against a pre-existing archive with no `deliveries` table (see Bugs/verification section below) — it sent all 12 once, then 0 on the next run.

### Failure-mode reasoning — 8/8

`digest.py:46-65`:
```python
for chan_cfg in cfg["channels"]:
    with db:
        if not dry_run:
            # Lock BEFORE checking the ledger, through send and commit, so
            # overlapping cron processes cannot both select the same items.
            db.execute("BEGIN IMMEDIATE")
        selected = store.pending_items(db, chan_cfg["name"], [...])
        if not selected:
            continue
        body = render.digest(selected, chan_cfg)
        if dry_run:
            ...
            continue
        channels.send(chan_cfg, body)
        store.record_delivery(db, chan_cfg["name"], selected)
    sent += len(selected)
```
Marking is per-channel, and the ledger write happens strictly after `channels.send` returns — correct ordering for at-least-once delivery, and the tradeoff is argued, not assumed (README.md:81-87): *"We record delivery **after** `channels.send` returns successfully, to avoid silently losing items on failed sends. This is **at-least-once**, not exactly-once... SQLite cannot atomically commit with an external webhook; exactly-once would require receiver-side idempotency support."*

I independently reproduced the partial-failure case (not just trusted the agent's own test) by patching `channels.send` to fail on the second channel:
```
channels attempted before crash: ['ops', 'energy']   # ops succeeded, energy raised
# next invocation:
second run sent: 9  channels: [('energy', 3), ('everything', 6)]   # ops NOT resent; energy + the never-attempted 'everything' both retried
```
That's exactly the behavior the README promises. I also reproduced the feed-outage case (wire down for one tick) and confirmed its items are neither lost nor duplicated once it recovers, and the overlapping-cron case with two real OS processes sharing one `--db` file (one with artificially slow sends): the second process blocked until the first finished and then correctly sent 0.

### Existing-code respect — 6/6

```
$ diff original/feeds.py run-1/feeds.py      # empty
$ diff original/channels.py run-1/channels.py # empty
$ diff original/render.py run-1/render.py     # empty
$ diff original/config.example.toml run-1/config.example.toml # empty
$ git diff --stat bafca98 -- digest.py store.py README.md
 README.md | 73 +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++--
 digest.py | 27 +++++++++++++++--------
 store.py  | 62 ++++++++++++++++++++++++++++++++++++++++++++++++-----
```
No wholesale rewrites. `--dry-run` was explicitly preserved and re-verified to not consume the ledger or write anything (`README.md:13-14`, confirmed by running it on a fresh DB — `items` got 6 archive rows, `deliveries` got 0, and the following real run still sent all 12). The pre-existing unconditional insert into the `items` archive (`store.record_items`, unchanged) is explicitly "dealt with" rather than ignored: dedup never reads from `items` at all, by design — `store.py:1-5`: *"An observation is not proof of delivery. Existing archive rows therefore never seed the ledger."* A new `tests/` directory was added; I don't count this as scope creep (it's not a feature, and the task didn't need to ask for it) — the rubric's scope-creep examples (plugin system, web UI, scheduler) describe added surface area, not test coverage for the thing being fixed.

### Code quality — 4/4

No dead code, no vestigial columns (`raw_id` on `items` is still populated and still serves the archive's stated purpose). Schema migration for an existing DB is a plain `CREATE TABLE IF NOT EXISTS deliveries`, which is sufficient since no existing column changed. SQL is parameterized throughout; the `deliveries` composite primary key `(channel, source, item_key)` doubles as the exact index the lookup query needs. `pending_items` does one `SELECT` per item rather than a batched `IN` query, which is an N+1 pattern in the abstract, but at fixture/production scale (a handful of items every 15 minutes) this is not a real defect, and I did not dock for it.

### Documentation — 4/4

`README.md` gained a `Deduplication decisions` section (identity + canonicalization rules + per-channel/per-source scoping), a `Success, failure, and overlapping cron runs` section (locking, ordering, at-least-once argument), and an `Existing databases / rollout` section (no backfill, first-run-sends-everything-once, told to preview with `--dry-run`). "How to reset" is answered, if a little indirectly — `README.md:40-42`: *"Deleting the database loses delivery history and causes current feed items to be sent again."* This is well beyond a docstring and clears all three things the rubric asks for.

## 3. What it missed

- **Unbounded archive/ledger growth.** Every poll inserts into `items` regardless of duplicates (pre-existing behavior, now joined by `deliveries`, which never shrinks either). Running every 15 minutes forever, both tables grow forever with no pruning or retention story. Not mentioned anywhere. This isn't a rubric category, but it's exactly the kind of decision "the next person to touch it" will eventually hit.
- **No scripted/parametrized way to reset dedup state for one channel or one feed.** The only documented reset is "delete the whole database," which also destroys the archive. An operator who wants to force-resend to a single misconfigured channel has to hand-write SQL against `deliveries`.
- **Cross-source duplicates are explicitly out of scope, but silently so for a reader who doesn't dig in.** `wire` and `newsroom` cover the same "port fee inquiry" story independently in the fixtures; both are sent (correctly, by the stated per-source design), but a reader skimming only the "Deduplication decisions" bullet list might not immediately realize cross-feed dedup was never attempted — it's stated ("Source names scope identity: two feeds covering the same story are independent") but easy to skim past.
- No discussion of what happens if `channels.send` partially succeeds from the receiver's point of view (e.g., webhook returns 200 after queuing but before actual delivery) beyond the general at-least-once framing — reasonable to leave out, but worth flagging as unexamined.

## 4. Bugs

None found. I specifically went looking for breakage in:
- Cross-process (not just cross-thread) locking, using two real `python3` processes against one `--db` file with an artificially slow `channels.send` — second process correctly blocked and then sent 0.
- The actual `webhook` channel type end-to-end against a real local HTTP server (the agent's own test suite only exercises `stdout` channels) — delivery and subsequent dedup both worked.
- A pre-existing database with an `items` archive but no `deliveries` table (simulating an in-place upgrade) — first run sent everything once, second run sent nothing, matching the documented rollout story.
- A feed outage (`wire` raising `FeedError`) followed by recovery — its items were neither lost nor duplicated.
- A mid-run channel failure (second of three channels raises) — the first channel's send was not repeated on retry, and the un-attempted third channel was retried along with the failed second one.

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
  "one_line": "Correct permalink-based dedupe with per-channel ledger, locking, and honest tradeoff docs."
}
```
