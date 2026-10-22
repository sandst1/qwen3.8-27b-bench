# Review: flint-nvfp4, run-2

## 1. Summary

The agent replaced raw-ID dedup with a normalized-link identity (strip
`utm_*`, lowercase host/scheme, sort remaining query params) and added a
`deliveries` ledger keyed per `(channel, source, link)`, so an item is sent to
a channel once and only once regardless of how many times the feed is
re-fetched. I verified this empirically against both fixture snapshots:
re-running the same snapshot sends zero duplicates, and moving from
snapshot-a to snapshot-b sends only the one genuinely new item, correctly
ignoring the rotated `utm_campaign`, the regenerated wire `guid`, and the
edited blogroll post. I would merge this with fixes: the mechanism is correct
and the existing code is respected, but the README omits how to reset state,
what a first run does, and what migrating an existing deployment's database
looks like, and `store.py` has a latent full-table-scan cost and an
unlabeled-link collision bug that the next person should know about before
this runs unattended for months.

## 2. Per-category scoring

### Identity strategy — 9/10

The identity function, `store.py:45-62`:

```python
def item_key(item):
    parts = urlsplit(item.get("link") or "")
    pairs = sorted(
        (k.lower(), v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
    )
    normalized = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(pairs), ""))
    return (item.get("source"), normalized)
```

This is a single uniform strategy (not a per-feed fallback chain), but it
happens to survive all three fixture failure modes because it deliberately
ignores the volatile field in each feed and keys on the one thing that stays
stable in the fixtures — the link:

- **newsroom**: `utm_campaign` rotates (`w33` → `w34`) on every item, every
  snapshot. Stripped by the `utm_` filter.
- **wire** (`generic` format): `guid` regenerates on edit
  (`wire-2026-08-14-0031` → `wire-2026-08-14-0031-r2`), but `link` doesn't
  change. The code never even looks at `raw_id` for identity.
- **blogroll**: has no `raw_id` at all (`feeds.py` sets it to `None`); identity
  falls back to the link, which is stable across the title/excerpt edit.

I confirmed this directly:

```
$ python3.13 digest.py --config config-b.toml --db digest.sqlite3
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
  https://newsroom.example/2026/08/port-fees-union?utm_source=feed&utm_campaign=w34
=== everything ===
Firehose — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```

Only the one actually-new newsroom story came through; the edited blogroll
post, the wire item with the new guid, and the newsroom item with the rotated
campaign were all correctly recognized as already-delivered.

Docked one point because the docstring states the *rationale* ("IDs are not a
stable identity") but not the new strategy's own failure modes — e.g. two
items from the same source that both lack a usable link collide (see Bugs,
below), and nothing says so.

### Ambiguity handling — 5/8

- **Per-channel vs. global suppression** — named and resolved explicitly, in
  both code and README. `store.py:26-32` keys `deliveries` on
  `(channel, source, link)`, and README says: *"Because the ledger is keyed
  per channel, a channel whose filters change picks up exactly the items it
  has not received before — old items it already got are not re-sent."* Top
  marks for this fork alone.
- **Edited item as new vs. same** — decided, but only implicitly. The
  docstring (`store.py:48-49`) says *"Feed providers rotate an item's ID
  whenever it is edited, so IDs are not a stable identity"* — which is the
  reason the code ends up treating edits as the same item — but nowhere does
  it say "therefore a corrected/updated item will not be redelivered,"
  which is the actual user-facing consequence a subscriber or operator would
  want to know. Verified: the blogroll post edited between snapshots ("Notes
  on port fee arithmetic" → same permalink, corrected table) never reappears.
- **First-run backfill** — not addressed anywhere, in code or docs. A fresh
  `digest.sqlite3` against snapshot-a sends all 12 matching items on the very
  first tick (verified below). This matches legacy behavior (the original
  code had no suppression at all), so it's not a regression, but it is a
  silent decision with real consequences on a freshly bootstrapped or
  migrated deployment, and the rubric calls it out by name as a fork to be
  named.

Two of three forks get *some* treatment (one excellent, one implicit), the
third is untouched. That's the "one or two named, rest decided silently"
band, toward its lower end because the "named" one that's merely implicit
isn't really surfaced as a decision with consequences.

### Failure-mode reasoning — 7/8

Per-channel marking is real, and the ordering is correct: mark-as-delivered
only happens after `channels.send()` returns without raising. `digest.py:46-68`:

```python
for chan_cfg in cfg["channels"]:
    # Each channel's selection, delivery and receipt recording share one
    # write transaction, so overlapping cron processes cannot both deliver
    # the same batch. Committing per channel keeps receipts of channels
    # that succeeded before a later channel failed.
    db.execute("BEGIN IMMEDIATE")
    try:
        pending = store.get_pending(db, chan_cfg["name"], all_items)
        selected = [i for i in pending if matches(i, chan_cfg)]
        if selected:
            body = render.digest(selected, chan_cfg)
            if dry_run:
                ...
            else:
                channels.send(chan_cfg, body)
                store.record_delivery(db, chan_cfg["name"], selected)
                sent += len(selected)
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
```

I tested the exact scenario the rubric asks about — channel one succeeds,
channel two fails — with a two-channel config (`energy` stdout first, `ops`
webhook to a closed port second):

```
$ python3.13 digest.py --config config-fail2.toml --db digest-fail2.sqlite3
... (energy prints its digest) ...
channels.DeliveryError: ops: <urlopen error [Errno 61] Connection refused>
$ sqlite3 digest-fail2.sqlite3 "select * from deliveries"
energy|newsroom|https://newsroom.example/2026/08/port-fees|...
energy|newsroom|https://newsroom.example/2026/08/offshore-tender|...
```

`energy`'s receipts survive the crash; `ops` will retry next tick without
having lost or duplicated anything. The `BEGIN IMMEDIATE` choice is also
explicitly argued in the comment above (guards against two overlapping cron
invocations both picking the same batch) — that is genuine tradeoff reasoning,
in code, which is what separates this from the 4–6 band.

Half a point held back because of a gap the agent didn't flag: an exception
in one channel still aborts the whole run, so *every channel after the failing
one in config order is skipped for that tick*, not just the failing one. I
confirmed this with a three-channel config (`energy`, `ops` [fails], `everything`):
`everything` never prints. This is inherited from the original code (verified
the same crash-aborts-remaining-channels behavior against the pristine
`digest.py`) so it isn't a new regression, but the agent restructured the loop
around per-channel transactions without using the opportunity to also stop a
down channel from blocking its downstream siblings for 15 minutes, and didn't
mention the limitation anywhere.

### Existing-code respect — 6/6

`feeds.py`, `channels.py`, `render.py`, `config.example.toml`, and
`fixtures/` are byte-for-byte untouched — confirmed with `diff`, all empty.
The `items` archive table is reused unchanged; the stated problem with reusing
it ("the unconditional insert" — see rubric §4) is dealt with directly in
`store.py:65-79`, which only inserts items not already present by identity,
rather than deleting/replacing the table or adding new unrelated tables beyond
the one `deliveries` ledger the fix actually needs. `--dry-run` still works
and still skips marking anything delivered (verified: running `--dry-run`
twice against the same data produces the same output both times, and a
`--dry-run` run doesn't affect a later real run's delivery count). No scope
creep: no new CLI flags beyond none-added, no plugin system, no web UI.

### Code quality — 3/4

Clean and readable, and reuses existing naming conventions (`FeedError`-style
module docstrings, same `conn.execute*` patterns). Two real issues:

- `store.py:65-69` re-derives every historical item's identity on **every
  run**, forever:

  ```python
  def record_items(conn, source, items):
      known = {
          (row["source"], item_key({"source": row["source"], "link": row["link"]})[1])
          for row in conn.execute("SELECT source, link FROM items")
      }
  ```

  The `items` archive only ever grows (it's described as "an archive of
  everything we have ever seen"), and this function does a full table scan
  plus a Python-side `urlsplit`/`parse_qsl` re-parse of every row, every 15
  minutes, forever. For a cron job meant to run indefinitely this is a
  scalability smell — a `UNIQUE` index on a normalized-link column plus
  `INSERT OR IGNORE` would do the same job in O(log n) per insert instead of
  O(total items ever seen) per run. `get_pending` (`store.py:82-100`) also
  issues one `SELECT` per candidate item rather than a single batched query;
  harmless at the fixture scale, worth a comment if this is going to see real
  volume.
- See Bugs below for the empty/missing-link identity collision this
  normalization scheme permits.

### Documentation — 2.5/4

The README was substantively rewritten, not just docstring-only (so it clears
the 2-point docstring cap), with a real "How sending works" section explaining
the ledger and the per-channel rationale — `README.md:31-42`. But it is
missing exactly the two things the rubric asks for by name:

- **How to reset it.** There's no mention of what to do if you want a channel
  (or everything) to be resent — e.g. `DELETE FROM deliveries WHERE channel =
  '...'` or "delete `digest.sqlite3` to start over." Grep for `reset` in the
  README, store.py, and digest.py returns nothing.
- **What happens on first run.** Not mentioned. A fresh database sends
  everything currently in the feed window as if it were all new, on the
  first tick ever. That's a reasonable default, but it's exactly the kind of
  silent decision the rubric wants surfaced, and it isn't.

Also unmentioned: upgrading an existing deployment. The schema change is
additive (`CREATE TABLE IF NOT EXISTS deliveries` alongside the untouched
`items` table) so it works without a migration script, which is good design,
but nobody told the next operator that upgrading in place will cause a
one-time re-delivery of whatever's currently in the feed window (since
`deliveries` starts empty) — operationally indistinguishable from the
first-run case above, and also unmentioned.

## 3. What it missed

- First-run backfill semantics (send everything vs. seed state silently) —
  decided by omission, not named.
- The in-place upgrade story (same consequence as first-run, not connected to
  it in the docs).
- A documented reset procedure.
- The fact that *content* edits — not just metadata churn — are invisible to
  subscribers once the link has been seen once. A subscriber who got the
  original blogroll post will never see the corrected version. This might be
  exactly the right call (comments scroll off, update-in-place is how blogs
  behave), but it's a real editorial tradeoff for a news digest tool, and
  nothing says it was made on purpose.
- The "one failing channel blocks every later channel for the rest of that
  tick" behavior wasn't fixed (it's pre-existing) or flagged, even though the
  agent restructured the exact loop where it lives.

## 4. Bugs

**Identity collision on missing/empty links.** `item_key` degrades to
`(source, "")` whenever `link` is falsy, so two distinct items from the same
feed that both lack a link collide into one identity, and the second is
silently dropped as "already delivered." Reproduction (no fixture triggers
this today, but `blogroll` is documented in `feeds.py` as having "no stable
identifier of any kind," making a missing link plausible for this exact
feed):

```
$ python3.13 -c "
import store
a = {'source': 'blogroll', 'link': ''}
b = {'source': 'blogroll', 'link': None}
print(store.item_key(a))
print(store.item_key(b))
print('collide:', store.item_key(a) == store.item_key(b))
"
('blogroll', '')
('blogroll', '')
collide: True
```

Not triggered by the current fixtures, so it didn't show up in testing, but
it's a latent correctness bug in exactly the feed the module's own comment
flags as the least trustworthy one.

**Not a bug but worth flagging as a near-miss:** a process crash between
`channels.send()` succeeding and `db.execute("COMMIT")` landing (e.g., `kill
-9` mid-write) loses the delivery mark for a message that was actually sent,
causing one duplicate on the next tick. This is the correct tradeoff for an
at-least-once system and is arguably unavoidable without transactional
delivery, but it's not named as a tradeoff anywhere, unlike the overlapping-
cron-process case which the code comment does call out.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 5,
  "failure_mode_reasoning": 7,
  "existing_code_respect": 6,
  "code_quality": 3,
  "documentation": 2.5,
  "total": 32.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid link-based identity and per-channel ledger; docs and scaling gaps remain."
}
```
