# Review: notify-digest dedup fix (ornith-122b-nvfp4, run-1)

## 1. Summary

The agent introduced a `sent(channel, item_key)` SQLite ledger, defined a single
cross-feed identity function `feeds.item_key()` keyed on `(source, canonical
link)`, and changed `digest.py` to filter each channel's candidates against its
own ledger before sending and to write the ledger only after a channel's send
succeeds. I reproduced the three adversarial fixture conditions (rotating
`utm_campaign`, a `guid` that regenerates on edit, a feed with no id at all)
by running `snapshot-a` then `snapshot-b` through the real code, and the
dedup behaves correctly in all three cases, including a simulated mid-batch
channel failure which neither lost nor duplicated a successfully-sent
channel's items on retry. I would merge this **with fixes**: the mechanism is
correct and well-isolated, but the README is silent on two things the rubric
and the task both care about — how to reset the ledger, and what happens the
first time this runs against the production box's *existing* database — and
the latter is not just a documentation gap, it's a reproducible one-time
mass-resend the day this ships.

## 2. Per-category scoring

### Identity strategy — 9 / 10

The agent picked one strategy for all three feeds instead of a per-feed
scheme, and it holds up against every fixture condition named in the prompt's
own `fixtures/` data:

```python
# feeds.py:87-96
def _canonical_link(link):
    """The article URL with query and fragment stripped.

    The newsroom feed rewrites its query every week (utm_campaign rolls over),
    so the raw link is not stable even though the article is. Dropping the
    query leaves the stable scheme+host+path. If a feed ever encodes identity
    in a query param this would miss it; none of ours do.
    """
    parts = urlsplit(link or "")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


# feeds.py:99-115
def item_key(item):
    """Stable identity for an item across runs, used to skip re-sends.

    The three feeds cannot agree on an identifier: newsroom has a stable
    entry_id (raw_id), the wire regenerates its guid on every edit, and the
    blogroll has no id at all. The one field present and stable across all
    three is the article URL, so we key on (source, canonical link).
    ...
    """
    canonical = _canonical_link(item.get("link")) or item.get("title", "")
    return f"{item.get('source', '')}\x00{canonical}"
```

I verified this empirically rather than just reading it. Running
`snapshot-a` then `snapshot-b` (copied to `/tmp/review-run1`, unmodified
agent code) through the real `digest.py` (not `--dry-run`):

```
$ python3 digest.py --config config.toml --db digest.sqlite3          # snapshot-a
sent 12 items
$ python3 digest.py --config config-b.toml --db digest.sqlite3        # snapshot-b
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
=== everything ===
Firehose — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```

Only the genuinely new newsroom story (`entry_id 84130`) is delivered.
The newsroom item whose `utm_campaign` rolled from `w33`→`w34`, the wire
item whose `guid` gained a `-r2` suffix, and the blogroll post retitled
`"(updated)"` with a changed excerpt are all correctly suppressed — exactly
the three traps the fixtures were built to contain.

Docked one point because this is a single universal heuristic rather than a
true fallback chain: newsroom's `entry_id` and the wire's `guid` are simply
discarded in favor of the URL, even where they'd be more precise. That's a
defensible, explicitly-argued tradeoff (and it is stated: "if a feed ever
encodes identity in a query param this would miss it"), not a bug — but it's
not quite the top-band "per-feed or fallback-chain" shape either.

### Ambiguity handling — 5.5 / 8

Two of the three canonical forks are named and resolved, in both code and
README:

- **Per-channel vs. global suppression** — named and resolved correctly:
  ```python
  # digest.py:45-47
  # Dedup is per channel: an item can legitimately go to several channels
  # (e.g. both "ops" and the "everything" firehose), and each channel must
  # remember only what it itself has already sent.
  ```
  Confirmed: in my snapshot-a run, the port-fee story went to both `ops` and
  `everything` in the same run — correct, each channel has its own ledger.

- **Edited item as new vs. same** — named and resolved correctly (quoted
  above in feeds.py and restated in README.md:43-45): edits are deliberately
  *not* resent, and the README says so explicitly.

The third fork — **first-run / backfill behaviour** — is not mentioned
anywhere, and it's the one that actually bites in this task's own scenario
("We run digest.py from cron every 15 minutes" implies an existing deployment
with an existing `digest.sqlite3`, not a fresh install). I simulated this:

```
$ sqlite3 old.sqlite3 "CREATE TABLE items(...)"   # pre-existing prod DB, no `sent` table
$ python3 digest.py --config config.toml --db old.sqlite3
=== ops ===       Ops digest — 3 item(s)
=== energy ===     Energy digest — 3 item(s)
=== everything ===  Firehose — 6 item(s)
sent 12 items
```

No crash, no migration error — `CREATE TABLE IF NOT EXISTS sent` handles the
schema fine. But because the new ledger starts empty, the very first run of
this patch against the real production database re-sends **everything
currently in every feed to every channel at once**, with no backfill, no
warning, and no mention in the README. For a fix whose entire purpose is
"people keep getting the same items over and over," shipping it with an
unannounced one-time flood on day one is the kind of silent decision the
rubric is pointing at. Two forks done well, one left completely unexamined
with a demonstrated real-world consequence — hence 5.5 rather than the top
of the 4–6 band.

### Failure-mode reasoning — 6.5 / 8

Marking is per-channel and happens only after that channel's send succeeds:

```python
# digest.py:55-62
body = render.digest(fresh, chan_cfg)
if dry_run:
    print(f"--- would send to {channel} ---")
    print(body)
    continue
channels.send(chan_cfg, body)
store.mark_sent(db, channel, [feeds.item_key(i) for i in fresh])
sent += len(fresh)
```

```python
# store.py:72-77
def mark_sent(conn, channel, keys):
    """Record that `keys` were delivered to `channel`.

    Called only after a channel's send succeeds, so a failed delivery is
    retried on the next cron tick rather than silently dropped.
    """
```

I tested the exact scenario the rubric calls out — channel two fails after
channel one succeeds — by monkeypatching `channels.send` to raise
`DeliveryError` for the `energy` channel only:

```
crashed as expected: simulated failure
sent_keys for ops:    {blogroll:..., newsroom:..., wire:...}   # marked, won't resend
sent_keys for energy: set()                                    # not marked, will retry
```

`ops` (which succeeded) is not re-sent on the next tick; `energy` (which
failed) is retried in full next tick, with nothing lost. This also correctly
composes with the *pre-existing, untouched* crash philosophy stated in
`channels.py`'s docstring ("if a channel is down we let the exception
propagate and cron will pick us up again") — `run_once`/`main` still don't
catch `DeliveryError`, so the whole run aborts on a channel failure exactly
as before, and the per-channel ledger is what makes that abort-and-retry safe
instead of lossy or duplicating.

This is a textbook at-least-once design, correctly implemented and correctly
tested by me against the stated failure case. The point loss is that the
*guarantee itself* — at-least-once, with a narrow crash window between
`channels.send()` returning and `mark_sent()`'s commit where a duplicate
could theoretically occur if the process is killed mid-write — is asserted
("so a failed delivery is retried...") but never named or weighed against the
alternative (mark-before-send, which would risk silently losing items
instead). The rubric's top band wants the tradeoff *argued*; here it's
implemented correctly but only half-argued.

### Existing-code respect — 5.5 / 6

`channels.py` and `render.py` are untouched. `feeds.py`'s existing `fetch()`
and the item shape it returns are untouched — the agent only appended two new
functions. `--dry-run` still works and, usefully, still correctly reflects
the ledger without mutating it (verified: dry-run after a real send shows 0
fresh items; dry-run on a fresh DB shows everything, repeatedly, since it
never writes `sent` — consistent, not a regression). The `items` archive's
pre-existing unconditional-insert-every-run behavior is left alone but is now
explicitly called out instead of being an unexplained surprise:

```python
# store.py:5-7
- `items` is an append-only archive of everything we have ever seen, so we can
  answer "did this ever come through?" when someone asks. It is not read during
  a normal run; duplicates across runs are expected and harmless.
```

That satisfies the rubric's "fine if the unconditional insert is dealt with"
bar via documentation rather than a code change, which is a reasonable choice
since nothing reads that table for dedup. No scope creep — no plugin system,
no extra CLI flags, no scheduler. Half a point off for the composite-key
encoding choice discussed below, which is a schema smell introduced while
extending the existing store rather than a new rewrite, so it lands here as
much as under code quality.

### Code quality — 3 / 4

The new code matches the existing style (docstrings-as-prose, same
executemany/commit pattern, same `Row` factory). Schema change is additive
and idempotent (`CREATE TABLE IF NOT EXISTS sent ...`), which is the correct
way to "migrate" an existing production `digest.sqlite3` — verified above,
no crash against a pre-existing DB missing the `sent` table.

The real wart is `item_key`'s encoding:

```python
# feeds.py:114-115
canonical = _canonical_link(item.get("link")) or item.get("title", "")
return f"{item.get('source', '')}\x00{canonical}"
```

Packing `(source, link)` into one `TEXT` column with an embedded NUL byte is
invisible to the standard tool anyone will reach for first during an
incident. Python-level binding and the SQLite B-tree comparison are fine
(verified — distinct keys differing only after the NUL do not collide, and
the `(channel, item_key)` PRIMARY KEY correctly rejects true duplicates), but
every SQLite *string function*, and the default CLI column display, treats
the value as C-string-terminated:

```
$ sqlite3 digest.sqlite3 "SELECT channel, item_key, sent_at FROM sent LIMIT 3;"
ops|blogroll|2026-10-20 19:55:02
ops|wire|2026-10-20 19:55:02
ops|newsroom|2026-10-20 19:55:02

$ sqlite3 digest.sqlite3 "SELECT length(item_key), quote(item_key) FROM sent LIMIT 1;"
8|'blogroll'
```

Anyone debugging "why didn't channel X get item Y" by opening the DB with
`sqlite3` — the obvious first move on a cron box — sees `item_key` values
that look like just the feed name, with the actual link silently gone from
view (it's still there, in the raw bytes — `hex(item_key)` shows it — but
nothing a human would normally run shows it). A two-column
`(channel, source, link)` composite key would have been no more code and
would not have this trap. This directly cuts against "leave the codebase in
a state the next person understands," so it costs a point here.

### Documentation — 2.5 / 4

This clears the "docstring alone caps at 2" floor — the dedupe rationale,
the chosen key, and its consequences are explained in both the `feeds.py`
docstring and a dedicated new README section:

```markdown
# README.md:31-49
## Delivery & dedup

Each run only sends a channel items it has not already received. "Same item"
is decided by `feeds.item_key()`, which keys on `(source, canonical link)` ...
Consequences worth knowing before you change this:
- A story is delivered to a given channel at most once, ever.
- An *edited* story ... keeps the same key and is therefore not re-sent.
- If an article moves to a new URL it is treated as new.
Dedup is per channel ... Delivery is recorded only after a channel's send
succeeds, so a failed delivery is retried on the next tick.
```

But the rubric asks specifically for three things and only one and a half
are here: the dedupe behaviour is explained well; **how to reset it is never
mentioned** (there's no `DELETE FROM sent WHERE channel=...` note, no
"delete `digest.sqlite3` to start over," nothing); and **what happens on
first run is never mentioned**, despite that being the single most
operationally relevant fact for whoever deploys this (see the migration
repro under Ambiguity handling — one unannounced flood to every channel).

## 3. What it missed

- **First-run / migration backfill.** Deploying this onto the real
  production `digest.sqlite3` (which already has history in `items`) causes
  every channel to receive everything currently in every feed in one go, the
  first time the new code runs, because the `sent` ledger starts empty and
  nothing seeds it from the existing `items` archive. Not discussed, not
  mitigated, not even flagged as a known limitation.
- **How to reset dedup for one channel or one item.** No documented
  operation for "someone wants channel X to get story Y again" (e.g., for a
  story that was wrongly suppressed, or a test). The obvious manual fix
  (`DELETE FROM sent WHERE channel='x' AND item_key=...`) is unusable without
  reading the source, because of the NUL-packed key noted above.
- **The crash-window duplicate case.** The at-least-once guarantee the code
  actually implements (send succeeds, process dies before `mark_sent`
  commits) is never named as a possibility, even though the adjacent
  "failed delivery is retried" sentence shows the agent was thinking about
  exactly this class of problem.
- **`raw_id`'s new role.** `raw_id` is still collected and stored in `items`
  but is no longer used anywhere for identity. That's fine (it was already
  just archival), but nothing says so — a future reader might reasonably
  assume `raw_id` is still load-bearing for dedup since it's sitting right
  next to `item_key`'s logic.

## 4. Bugs

No functional dedup bug was found — the mechanism was tested against both
fixture snapshots and a simulated partial-channel-failure and behaved
correctly in every case I tried. The one defect worth flagging as a near-bug
rather than a documentation gap:

**`item_key` is unreadable through the standard sqlite3 CLI**, because it
concatenates `source` and the canonical link with an embedded `\x00` and
every ordinary SQLite string function (and the CLI's default row display)
treats `TEXT` as NUL-terminated:

```
$ sqlite3 digest.sqlite3 "SELECT channel, item_key FROM sent LIMIT 1;"
ops|blogroll
$ sqlite3 digest.sqlite3 "SELECT length(item_key) FROM sent LIMIT 1;"
8
```

Reproduction: run the agent's `digest.py` once against
`config.example.toml` (which points at `fixtures/snapshot-a`), then open the
resulting `digest.sqlite3` with the `sqlite3` CLI and `SELECT * FROM sent` —
every row's `item_key` displays as just the feed name, with no indication the
link portion exists at all. Underlying data and dedup logic are correct
(verified via `hex(item_key)` and via Python bound-parameter equality tests);
this is a debuggability trap, not a correctness bug, but it is exactly the
kind of thing that violates "leave the codebase in a state where the next
person understands what you chose and why" if that person's first move is to
look at the table.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 5.5,
  "failure_mode_reasoning": 6.5,
  "existing_code_respect": 5.5,
  "code_quality": 3,
  "documentation": 2.5,
  "total": 32,
  "would_merge": "yes-with-fixes",
  "one_line": "Correct, tested per-channel dedup; silent on first-run flood and an opaque debug key."
}
```
