# Review: notify-digest dedupe fix

## 1. Summary

The agent replaced the write-only `items` archive with a per-channel `sent`
ledger (`store.py`) and a new `feeds.item_key()` that identifies a story by
`source + link host/path` (query string stripped), falling back to
`raw_id`/`title` when a feed has no link; `digest.py` now filters each
channel's candidates against that channel's own ledger and only records a
row after `channels.send()` returns without raising. I ran it against both
fixture snapshots and against a synthetic partial-failure case; the core
mechanism — no repeats within a snapshot, no repeats across the utm/guid/title
edits in snapshot-b, per-channel isolation, surviving a mid-run webhook
failure without re-sending to the channel that already got through — all
checked out. I would merge this with fixes: the implementation is sound, but
the docs state a retry guarantee that the code provably does not have, the
existing-database migration story is incomplete (a production `digest.sqlite3`
is left with a permanently dead `items` table), and first-run backfill
behavior is never mentioned despite being a real deployment surprise.

## 2. Per-category scoring

### Identity strategy — 9 / 10

`feeds.py:90-111`:

```python
def item_key(item):
    """Stable identity of an item across fetches.

    Keyed on source + link host/path, because neither field you would
    naively reach for is stable in our feeds:

    - the generic provider regenerates the guid whenever an item is edited
      (see "wire-...-r2" in fixtures/snapshot-b), so guid == new is wrong;
    - the newsroom rotates the utm_* tracking params in its URLs between
      snapshots, so the full URL (and its hash) is not stable either.

    An item re-published at the same URL after an edit is deliberately not
    treated as new news. Feeds that ship an empty link fall back to
    raw_id/title so their keys stay distinct instead of colliding on "".
    """
    link = (item.get("link") or "").strip()
    if link:
        parts = urlsplit(link)
        ident = f"{parts.netloc.lower()}{parts.path.rstrip('/')}"
    else:
        ident = item.get("raw_id") or item.get("title", "")
    return f"{item['source']}|{ident}"
```

This is exactly the fallback-chain the top band asks for, and the docstring
states the failure modes it exists to defeat. I verified all three against
the fixtures by running snapshot-a then snapshot-b through the same db
(`/tmp/notify-test`, copy of this directory):

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ sed -i '' 's/snapshot-a/snapshot-b/g' config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-b
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
=== everything ===
Firehose — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```

Only the genuinely new newsroom entry (`entry_id` 84130) went out. The wire
item whose `guid` grew an `-r2` suffix and gained a sentence, the blogroll
post whose title grew `(updated)` and whose excerpt changed, and the two
newsroom items whose `utm_campaign` rotated from `w33` to `w34` were all
correctly suppressed — none of the three failure modes named in the
docstring broke it.

Docked one point because the strategy is a single uniform rule (link
host+path for everyone) rather than genuinely per-feed identity. The
newsroom's `entry_id` (`feeds.py:56`, `str(r["entry_id"])`) is, per the
original module docstring, never called out as unstable the way `guid` is —
it would have been a stronger fallback chain to prefer it for that feed
specifically instead of relying on URL-path stability holding forever. Not a
fixture-breaking gap today, but the one-size-fits-all choice is unargued.

### Ambiguity handling — 6 / 8

Two of the three canonical forks are named and resolved explicitly, in two
places each:

- **Per-channel vs. global** — `store.py:9-12`: *"The ledger is per-channel,
  not global. Two channels are two audiences; an item one of them received
  tells us nothing about the other."* Verified: after a clean first run,
  `sent` has independent row counts per channel (`ops:3, energy:3,
  everything:6`) even though `everything`'s items overlap with the other two.
- **Edited-as-new vs. same** — `feeds.py:101-102`: *"An item re-published at
  the same URL after an edit is deliberately not treated as new news."*
  Confirmed by the snapshot-a→b run above: the edited wire and blogroll items
  did not re-fire.

The third fork — **first-run backfill** — is not named anywhere. I grepped
the README, every module, and the agent's own log for `first run`,
`backfill`, `first-run`: zero hits. The actual behavior (verified above) is
that an empty ledger sends the *entire current rolling window* of every feed
on the first invocation — in the fixtures, 12 items in one digest run. On a
real deployment this is the moment the operator discovers how big the
feeds' rolling window actually is, with no warning in the code or docs that
this is what "the first tick after deploying this" looks like. It is a
defensible default, just an entirely silent one, which is exactly the
"4–6: one or two named, the rest decided silently but correctly" band.

### Failure-mode reasoning — 6 / 8

The mechanism is right: `digest.py:54-70`

```python
for chan_cfg in cfg["channels"]:
    already = store.sent_keys(db, chan_cfg["name"])
    selected = [i for i in all_items
                if feeds.item_key(i) not in already and matches(i, chan_cfg)]
    if not selected:
        continue
    body = render.digest(selected, chan_cfg)
    if dry_run:
        ...
        continue
    channels.send(chan_cfg, body)
    store.record_sent(db, chan_cfg["name"], [(feeds.item_key(i), i) for i in selected])
    sent += len(selected)
```

`record_sent` runs only after `channels.send` returns, and marking is scoped
to the channel just processed — so an exception on channel 2 does not touch
channel 1's already-committed rows. I reproduced the exact scenario the
rubric asks about (channel two fails after channel one succeeded) with a
`stdout` channel first and a `webhook` pointed at a closed port second:

```
RUN 1: ops prints its 3-item digest; energy raises DeliveryError, process exits 1.
DB after RUN 1: sent has 3 rows, all channel='ops'. Zero 'energy' rows.
RUN 2 (energy still down): ops prints NOTHING.
```

That's the correct, desirable outcome — the already-delivered channel is not
re-sent, the broken one keeps retrying. But the stated justification for
this is wrong. `store.py:13-16`:

```
- Rows are written only after a successful send. If a webhook is down the
  run dies with DeliveryError (see channels.py) and nothing is recorded,
  so the next tick retries the batch — including items that had already
  gone out to an earlier channel this run.
```

That last clause ("including items that had already gone out to an earlier
channel this run") is the opposite of what RUN 2 above actually does: `ops`'s
items were *excluded*, not retried. `digest.py:38-40`'s docstring has the
same ambiguity ("if delivery dies mid-run, nothing is marked"). A future
maintainer debugging "why didn't ops get item X on the retry tick" who reads
either docstring will be told the wrong story about their own system. The
ordering is correct and the at-least-once-for-the-failing-channel tradeoff is
argued, but the argument as written misdescribes the implementation, which is
exactly the risk the prompt's "so the next person understands what you chose
and why" was guarding against. I also note the original channel-ordering
design — unchanged, not the agent's doing — means a persistently broken
channel blocks every channel listed after it, every tick, forever; the
README's claim that this "is accepted so that one dead channel cannot
silently swallow items from the others" (README.md:33-36) is only true for
channels before the broken one in `cfg["channels"]`, which isn't flagged.

### Existing-code respect — 4 / 6

`channels.py`, `render.py`, `config.example.toml`, and the three feed-format
branches in `feeds.py` (lines 47-87) are byte-for-byte unchanged — confirmed
with `diff`. `--dry-run` still short-circuits before any `channels.send` or
`store.record_sent` call and still doesn't persist state (verified: two
consecutive `--dry-run` runs produce identical output and zero ledger rows).
`digest.py`'s loop structure, `matches()`, and sort are untouched; the dedupe
filter is a minimal, additive insert into the existing loop.

`store.py`, however, was rewritten wholesale rather than extended. The
rubric explicitly flags the alternative: *"Reusing the `items` archive is
fine if the unconditional insert is dealt with."* The agent instead deleted
the archive concept outright — `record_items`/`count_items` and the `items`
table are gone, replaced by `sent`. The stated reason, `store.py:22-23`
(*"The pre-2026 schema had an `items` archive table that nothing read; new
databases simply do not have it"*), only covers *new* databases. This job
runs from cron against a long-lived production `digest.sqlite3`. I simulated
that: created a db with the old `items` schema and one row, then ran the new
code's `--dry-run` against it —

```
before: .tables -> items
after:  .tables -> items  sent
        items row count: 1   (unchanged, untouched, never read again)
```

The old table is not dropped, not migrated, not mentioned as something an
operator should clean up — it just becomes permanent dead weight on every
existing deployment the moment this patch lands. That is the scenario the
rubric calls out by name, and it was not dealt with.

### Code quality — 2.5 / 4

The new code itself is clean: `sent_keys`/`record_sent` are short, the
`PRIMARY KEY (channel, item_key)` + `INSERT OR IGNORE` pattern
(`store.py:29-36, 59-63`) is the right idempotent primitive for this table,
and naming is clear. But the rubric names "schema migration handled for an
existing database" as part of this category, and as shown above it is not
handled — no `DROP TABLE items`, no `ALTER`, no runtime check, no note to
ops. For a job explicitly described as running against a persistent
cron-managed sqlite file, that's a real gap, not a hypothetical one.

### Documentation — 2.5 / 4

README gets a genuinely substantial new section, `README.md:15-36`
("No repeats"), that explains the dedupe key, both failure modes it guards
against, and the per-channel ledger choice — well above "docstring alone."
But of the three things the rubric asks the README to cover, only one
(dedupe behavior) is actually there. "How to reset it" is never mentioned —
there's no note that deleting `digest.sqlite3` or clearing the `sent` table
re-arms every channel, which is the first thing an operator chasing a
missing digest will want to know. "What happens on first run" is also never
mentioned, despite it being a real backfill event (see Ambiguity handling
above). And the one failure-mode claim it does make about retries
(`README.md:33-36`) is the same incorrect one scored above.

## 3. What it missed

- **First-run backfill size.** Nothing flags that deploying this sends every
  item currently in each feed's rolling window as if brand-new. If a feed's
  window is big, the first tick after rollout is a large, unannounced blast.
- **Existing-database migration.** The `items` table is left on disk forever
  for any pre-existing deployment, unreferenced and undocumented, exactly the
  scenario the rubric singles out.
- **Audit trail regression.** The old `items` table recorded *everything
  seen*, including stories that matched no channel's keywords. The new
  `sent` table only records what was actually delivered — "did we ever see
  story X at all, even if nobody got it" is a question this version can no
  longer answer, and that capability loss is not mentioned anywhere as a
  tradeoff, only justified by "nothing read it [before]."
- **Head-of-list channel failures blocking the rest.** Inherited from the
  original `channels.py` design, but now has sharper teeth: a persistently
  broken channel anywhere but last in `cfg["channels"]` prevents every later
  channel from being attempted on every tick until it's fixed or reordered.
  The README's "one dead channel cannot silently swallow items from the
  others" line doesn't hold for channels listed after the dead one.
- **Crash between delivery and commit.** If the process is killed between
  `channels.send()` returning and `store.record_sent()`'s `conn.commit()`
  (e.g., a cron timeout kill), that channel will re-deliver on the next tick.
  Not discussed; a minor, probably acceptable gap, but silent.

## 4. Bugs

**Documentation bug, reproducible:** `store.py:13-16` and `digest.py:38-40`
both claim that when a later channel fails mid-run, the retry on the next
tick re-includes items an earlier, already-successful channel received.
That's false. Reproduction (from a clean db, `ops`=`stdout`, `energy`=
`webhook` pointed at a closed port, in that order):

```sh
$ python3 digest.py --config config_fail.toml --db digest.sqlite3
=== ops ===
Ops digest — 3 item(s)
...
channels.DeliveryError: energy: <urlopen error [Errno 61] Connection refused>

$ sqlite3 digest.sqlite3 "select channel from sent;"
ops
ops
ops

$ python3 digest.py --config config_fail.toml --db digest.sqlite3   # next tick
# (energy still down)
channels.DeliveryError: energy: <urlopen error [Errno 61] Connection refused>
# ops prints NOTHING this time — its items were correctly excluded, not retried
```

The implementation is correct; the two docstrings describing it are not.
Anyone trusting the comment instead of reading the code will draw the wrong
conclusion about what a partial failure does.

**Not a bug, but worth flagging as a trap:** running against an existing
production `digest.sqlite3` silently leaves the old `items` table in place,
untouched and unreadable, forever (reproduced above under Existing-code
respect). Nothing crashes, nothing warns — it's just permanent clutter with
no documented cleanup path.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 6,
  "existing_code_respect": 4,
  "code_quality": 2.5,
  "documentation": 2.5,
  "total": 30,
  "would_merge": "yes-with-fixes",
  "one_line": "Correct per-channel link-based dedupe; retry docs wrong, no DB migration or first-run note."
}
```
