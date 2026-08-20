# Review — notify-digest dedupe fix

## 1. Summary

The agent added a `sent_items` table keyed on `(source, normalized-link)` and
gated delivery through a `pending_items()` check, marking items sent only
after the per-channel send loop finishes for the whole run. The identity
strategy (URL with query string stripped) is well-reasoned and verified to
survive all three feeds' quirks (rotating `utm_campaign`, missing `raw_id`,
regenerated `guid`), but the "mark as sent" step operates on the *whole run's*
pending set rather than per-channel, which both silently drops items that
never matched any channel filter and duplicates delivery to channels that
already succeeded when a later channel in the loop fails. I would merge this
with fixes: the identity work is genuinely good, but the marking bug is a
real correctness regression that must be closed before it goes back on cron.

## 2. Per-category scoring

### Identity strategy — 9/10

`store.py` picks source + normalized link (query string dropped) as identity,
and states why in the module docstring:

```python
"""
Identity. An item is identified by its (source, link) pair, where `link` is the
item's URL with the query string dropped. We deliberately do NOT use the
providers' own ids as identity:

    * newsroom exposes a stable entry_id, but its link carries a changing
      `utm_campaign` param, so the raw link is not stable.
    * blogroll has no id at all.
    * the generic wire regenerates its `guid` every time an item is edited.
```

```python
def link_key(link):
    """Canonical identity for a URL: scheme + host + path, tracking stripped."""
    parts = urlsplit(link or "")
    return f"{parts.scheme}://{parts.netloc}{parts.path}"
```

I verified this against the fixtures directly. Snapshot-a → snapshot-b bumps
`utm_campaign=w33` to `w34` on *every* newsroom item (not just the edited
one), which would break naive link-equality dedupe across the board; with the
query stripped, only the genuinely new item ("Union responds to port fee
inquiry") is delivered on the second run:

```
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
```

Blogroll's title/excerpt edit (no id, same permalink) and wire's guid bump
(`wire-2026-08-14-0031` → `-r2`, same link) both correctly resolve to "same
item, don't resend." Docked half a point because stripping the *entire*
query string is coarse — a feed that legitimately encodes identity in a query
param (e.g. `?id=`) would collide under this scheme, and that risk isn't
flagged anywhere.

### Ambiguity handling — 3/8

Of the three canonical forks:

- **Edited item as new vs. same**: resolved explicitly and correctly (link-based
  identity, documented above) — this is the one credit-worthy fork.
- **First-run backfill**: not mentioned anywhere. On a virgin database every
  historical item in the feed goes out in one digest; no comment, README
  note, or flag addresses whether that's intended.
- **Per-channel vs. global suppression**: resolved *silently* and *incorrectly*.
  `run_once` computes `pending` once, globally, then marks the *entire*
  pending set as sent after the channel loop — regardless of whether an item
  actually matched any channel's filter:

  ```python
  pending = store.pending_items(db, all_items)
  ...
  for chan_cfg in cfg["channels"]:
      selected = [i for i in pending if matches(i, chan_cfg)]
      ...
  if not dry_run:
      store.mark_sent(db, pending)   # marks ALL of `pending`, not `selected`
  ```

  I reproduced this: with a channel whose keywords match nothing in the feed,
  the run reports `sent 0 items` but `sent_items` fills up anyway:

  ```
  sent 0 items
  === sent_items table after run ===
  {'source': 'blogroll', 'link': 'https://blog.example/port-fee-arithmetic', ...}
  {'source': 'wire', 'link': 'https://wire.example/i/0031', ...}
  ... (all 6 items)
  ```

  Every item the feed produced is now permanently marked "sent," even though
  it was delivered to nobody. If a channel with different keywords is added
  later, these items are gone for good. This is exactly the "global
  suppression, decided silently, and wrong" failure the rubric calls out.

### Failure-mode reasoning — 3/8

There is a comment arguing for at-least-once delivery:

```python
# Mark after delivering so a failed send leaves the run uncommitted and
# cron retries it next tick instead of skipping the item forever.
if not dry_run:
    store.mark_sent(db, pending)
```

But the marking is per-*run*, not per-*channel*, so the stated guarantee
doesn't hold across a partial failure. I reproduced this with two channels,
the first healthy and the second pointed at a dead webhook:

```
=== ops ===
Ops digest — 1 item(s)
* Port fee inquiry opened  [wire]
...
channels.DeliveryError: broken: <urlopen error [Errno 61] Connection refused>
exit: 1
```

`mark_sent` is never reached because the exception propagates out of
`run_once` before it. Re-running with the same broken config re-sends the
same item to `ops` — which already received it — a second time:

```
=== run again (ops should re-send since mark_sent never ran) ===
=== ops ===
Ops digest — 1 item(s)
* Port fee inquiry opened  [wire]
...
```

So channel one gets the item twice while channel two (once fixed) gets it
once. This is the "per-run marking ... loses or duplicates a whole batch on
partial failure" band — the tradeoff is gestured at in a comment but the
implementation doesn't actually deliver on the stated guarantee, and the
per-channel case isn't discussed at all.

### Existing-code respect — 5.5/6

Diff is minimal and surgical: `feeds.py`, `channels.py`, `render.py` are
untouched. The pre-existing `items` archive table and its unconditional
insert are left alone and correctly kept out of the delivery-suppression path
(the docstring explicitly separates the two concerns). `--dry-run` is
respected — it skips both `record_items` and `mark_sent`, verified: two
consecutive dry-runs against the same DB produce identical output, and a
following real run still sees all items as pending. Half point off because
the archive-write is now gated behind `if not dry_run` where it previously
ran unconditionally on `--dry-run` too — a silent, unremarked behavior change
to an existing code path.

### Code quality — 3.5/4

Clean, small additions; `sent_items` schema uses a sane composite primary key
and `INSERT OR REPLACE`; no dead code or vestigial columns. No explicit
migration is needed since the new table is additive
(`CREATE TABLE IF NOT EXISTS`), which the code correctly relies on rather than
writing a migration nobody needs. Docked half a point for the bug baked into
otherwise readable code: `store.mark_sent(db, pending)` should be
`store.mark_sent(db, actually_sent)` where `actually_sent` accumulates only
items that were in a channel's `selected` list — as written, the variable
name `pending` masks the fact that it's marking un-delivered items too.

### Documentation — 2/4

`README.md` is byte-for-byte unchanged — no mention of the new `sent_items`
table, how to reset/replay a digest, or first-run behavior. All the reasoning
lives in the `store.py` module docstring and two inline comments in
`digest.py`. Per the rubric, "a docstring alone caps this at 2."

## 3. What it missed

- **First-run backfill** is not addressed at all, silently or otherwise: a
  fresh database sends every historical item in the feed in one digest.
- **Per-channel vs. global suppression** was decided, but wrongly and without
  comment — see the Ambiguity Handling section. Items that match no channel
  are marked sent regardless.
- **Reset/replay story**: nothing in code or docs says how an operator would
  force a resend (e.g., delete rows from `sent_items`, or a `--replay` flag).
- No update to `config.example.toml` or `README.md`'s table of files to
  reflect the new table/behavior.

## 4. Bugs

**Bug 1 — items are marked "sent" even when delivered to zero channels.**
Reproduction (channel keyword that matches nothing in the fixtures):
```
$ python3 digest.py --config config-test.toml --db test2.sqlite3
sent 0 items
$ sqlite3 test2.sqlite3 "select * from sent_items"
blogroll|https://blog.example/port-fee-arithmetic|...
wire|https://wire.example/i/0031|...
newsroom|https://newsroom.example/2026/08/port-fees|...
... (all 6 rows)
```
Cause: `store.mark_sent(db, pending)` in `digest.py` marks the full
`pending` list, not the union of each channel's `selected` list.

**Bug 2 — partial channel failure duplicates delivery to channels that already
succeeded.** Reproduction (two channels, second one unreachable):
```
$ python3 digest.py --config config-fail.toml --db test3.sqlite3
=== ops ===  (delivered)
...DeliveryError: broken: Connection refused
$ python3 digest.py --config config-fail.toml --db test3.sqlite3   # retry
=== ops ===  (delivered again — duplicate)
...DeliveryError: broken: Connection refused
```
Cause: same root issue — marking happens once, globally, after the entire
channel loop, so a failure partway through the loop leaves nothing marked,
and the next cron tick re-sends to channels that already got the item.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 3,
  "failure_mode_reasoning": 3,
  "existing_code_respect": 5.5,
  "code_quality": 3.5,
  "documentation": 2,
  "total": 26,
  "would_merge": "yes-with-fixes",
  "one_line": "Good link-based identity, but batch-level marking causes silent drops and duplicate sends"
}
```
