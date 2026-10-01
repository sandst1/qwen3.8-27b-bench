# Review: notify-digest dedup fix

## 1. Summary

The agent replaced the dead `items` archive table with a per-channel
`delivered` ledger keyed on a normalised URL (origin+path, query and
fragment stripped), moved the dedupe check and the delivery mark into
`digest.py`'s per-channel send loop, and wrote five regression tests plus a
substantial README section explaining the identity choice, the per-channel
scope, and the retry behaviour on failure. I ran the agent's own test suite
(5/5 pass) and replayed `snapshot-a` then `snapshot-b` by hand against a
shared db — the three documented feed quirks (rotating `utm_campaign`,
regenerated `guid`, no id at all) are all correctly deduped, and the one
genuinely new story in `snapshot-b` is the only thing sent. I would merge
this: it is correct, it is scoped to the stated problem, it doesn't touch
`feeds.py`/`channels.py`/`render.py`, and it explains itself — the main
gaps are a left-behind legacy table on upgrade and a README that doesn't
spell out the first-run flood.

## 2. Per-category scoring

### Identity strategy — 9.5 / 10

`store.py:49-61`:

```python
def item_key(item):
    """The ledger key for an item: its URL minus query and fragment.
    ...
    """
    link = (item.get("link") or "").strip()
    parts = urlsplit(link)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"
    return link or f"raw:{item.get('raw_id') or ''}"
```

This is a single rule (origin+path) with a fallback for feeds that give no
usable link, which is a fallback chain in substance even though it's one
function. It survives all three quirks named in the original `feeds.py`
docstring, and I verified this by running the real fixtures end to end:

```
$ python3 digest.py --config config-a.toml --db digest.sqlite3   # first run
sent 12 channel-item deliveries
$ python3 digest.py --config config-a.toml --db digest.sqlite3   # repeat
sent 0 channel-item deliveries
$ python3 digest.py --config config-b.toml --db digest.sqlite3   # 4h later
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
...
sent 2 channel-item deliveries
```

Snapshot B re-serves the newsroom items under a changed `utm_campaign`,
the wire item under a regenerated `guid` (`wire-...-0031` →
`wire-...-0031-r2`, same `link`), and a retitled blogroll post under the
same `permalink`. All three are correctly suppressed; only the one story
with a genuinely new URL (`port-fees-union`) goes out. The failure mode is
stated explicitly, not just implied (`store.py:16-18`):

```
Known trade-off: a provider that identifies distinct stories
via query string (…/article?id=N) would be collapsed to one item by
this rule; none of our three providers does that today.
```

This is close to a direct match for the rubric's 9–10 description. I'm
docking half a point only because the "fallback chain" is really a single
rule plus a degenerate fallback, not a per-feed strategy — it happens to
work because none of the three fixture feeds currently need query-string
disambiguation, and that's acknowledged rather than engineered around.

### Ambiguity handling — 6 / 8

Two of the three canonical forks are named and resolved explicitly, with
tests to back them up.

Per-channel vs. global, `store.py:19-21`:
```
* The ledger is keyed per channel, not globally. "Already seen" is a
  fact about an audience: a story already delivered to #ops is still
  news to #firehose.
```
Tested in `test_digest.py:102-120` (`test_channel_that_missed_stories_still_gets_them`).

Edited-item-as-new-vs-same, `README.md` ("No repeats" section) and
`store.py:10-18`: the blogroll post retitled `"(updated)"` under the same
`permalink` in `fixtures/snapshot-b/blogroll.json` is treated as the same
story and verified not to re-send in `test_digest.py:85-100`
(`test_rehashed_duplicates_are_not_resent`).

The third fork — first-run backfill — is not named anywhere. On an empty
ledger every item in the feed's current window goes out in one digest;
that's the only sane default, and the agent's own test exercises it
(`first = digest.run_once(...); self.assertGreater(first, 0)` in
`test_digest.py:78-83`), but neither the code nor the README says "the
first run will flush the whole current window, and that's expected." That
puts this at the top of the "one or two named, the rest decided silently
but correctly" band rather than all three.

### Failure-mode reasoning — 7 / 8

Marking is per-channel and ordered send-then-mark, and the loop commits
once per channel rather than once per run (`digest.py:63-67`, `store.py:70-81`):

```python
# digest.py
# If the send raises, nothing is recorded and the next cron tick
# retries these items.
channels.send(chan_cfg, body)
store.mark_delivered(db, chan_cfg["name"], selected)
```
```python
# store.py
def mark_delivered(conn, channel, items):
    """Record that `channel` received `items`, and commit immediately.

    Committing per channel is deliberate: if a later channel's send fails
    and kills the process, the channels that already got their digest must
    not be sent it again on the next cron tick.
    """
```

I reproduced the "channel two fails after channel one succeeded" case by
hand: with `ops` sending fine and `energy` raising `DeliveryError`, `ops`'s
3 items were committed to `delivered` before the exception propagated and
killed the run; `everything` (which runs after `energy` in config order)
never got a chance to send at all. On the next tick, `ops` correctly sent
nothing new, `energy` and `everything` both resent/sent their full
backlog. That is a deliberate, argued choice (retry favours duplicates
over silent drops), and it's stated in both `store.py:23-25` and the
README. I'm not giving this the full 8 because the argument is about the
*exception* path only — a hard process kill (`kill -9`, OOM) between
`channels.send` returning and `store.mark_delivered`'s commit produces the
same at-least-once duplicate, and that residual window is never named,
even though it's the more interesting failure mode once you've already
handled the exception case.

### Existing-code respect — 5 / 6

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are
byte-identical to the original (verified with `diff`, no output). The
change is contained to `store.py` and `digest.py`, and `--dry-run` was
updated correctly to filter against the ledger but skip writing to it —
I confirmed this does not burn items (`test_digest.py:136-143`, and by
hand: a `--dry-run` pass followed by a real pass still delivers
everything).

The rubric says reusing the `items` archive is fine *if* the unconditional
insert is dealt with; the agent instead deleted the archive outright and
replaced it with `delivered`. That's a legitimate design call — the old
table was explicitly dead code ("Nothing reads it at the moment") — but it
silently drops the "did this story ever come through, for anyone"
audit capability the original docstring described as the table's purpose,
and neither the code nor the README mentions that capability was removed.
Half a point off for that silent removal, not for the replacement itself.

### Code quality — 3 / 4

The new schema is lean and the SQL is sensible — composite primary key,
`WITHOUT ROWID`, and `INSERT OR IGNORE` to make `mark_delivered` safe
against being called twice with overlapping keys (`store.py:32-37`,
`:77-80`). No leftover `count_items`/`record_items` dead code; the removed
functions are gone, not stubbed out.

What's missing is schema migration for a database that already exists
from a previous deployment. I simulated this: a pre-existing
`digest.sqlite3` with the old `items` table loads fine (no crash — `CREATE
TABLE IF NOT EXISTS` just adds `delivered` alongside it), but the old
`items` table and its rows are now permanently orphaned, with nothing in
code or docs telling an operator it's safe to drop. That's exactly the
"vestigial table left behind on upgrade" the rubric calls out under this
category.

### Documentation — 3 / 4

This clears the "docstring alone caps at 2" floor by a wide margin — the
README gets a dedicated section (`README.md`, "No repeats: how 'new' is
decided") that explains the URL-based identity with the same concrete
per-provider justification as the code comment, explains per-channel
scope, and explains the retry-on-failure behaviour:

```
If a send fails, the run aborts before anything is recorded for that
channel, so the next tick retries the undelivered items. To replay
history — e.g. after adding a new channel — delete the db file.
```

That covers "how to reset it" explicitly. It does not cover "what happens
on first run" — nothing says that the very first invocation against an
empty db will flush the entire current window of every feed in one
digest, which for a real feed (not the two-item fixtures) could be a
large and possibly alarming first message. Given the rubric names that as
one of the three things the README should cover, I'm taking a point off
for the gap rather than giving this full marks.

## 3. What it missed

- **First-run flood**, as above — undocumented and unnamed, though
  correctly handled by omission (empty ledger = send everything once).
- **Loss of the audit archive.** The original `items` table existed so
  someone could ask "did this ever come through?" later. That capability
  is gone and nobody is told.
- **Upgrade path for existing deployments.** A box already running the old
  code has an `items` table that becomes permanently dead weight after
  this patch; nothing drops it, comments on it, or tells the operator it's
  safe to drop.
- **Crash-between-send-and-commit window.** The failure-mode writeup
  argues the exception path (channel raises, nothing recorded) but doesn't
  acknowledge the residual case where the process itself dies between a
  successful `channels.send` and the following `commit()` — which is also
  an at-least-once duplicate, just a less likely one.
- **Log-message format change.** `main()` now prints `"sent N channel-item
  deliveries"` instead of `"sent N items"` (`digest.py:83`). More accurate
  (the old number double-counted an item sent to two channels), but it's
  an unannounced change to the one thing cron logs that an on-call person
  might grep for.

## 4. Bugs

None found. I ran:
- The agent's own suite: `python3 -m unittest test_digest -v` → 5/5 pass.
- A manual replay of `snapshot-a` → `snapshot-a` (repeat) → `snapshot-b`
  against one db file: correct 12 → 0 → 2 delivery counts, and the two
  "sent" items in the last run are exactly the one new story, to the two
  channels it matches.
- A manual partial-failure scenario (second channel's `send` raises):
  confirmed the first channel's items are committed to `delivered` before
  the exception propagates, and that on retry only the failed/unattempted
  channels resend — matching the stated per-channel, at-least-once
  contract.
- A manual `--dry-run` followed by a real run: the dry run does not
  consume any ledger entries, matching `test_dry_run_leaves_ledger_untouched`.
- A manual migration simulation (pre-seeded old-schema `items` table,
  then run the new code against it): no crash, but the old table is left
  behind, unused (see "Code quality" above — not a bug, a vestige).

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 7,
  "existing_code_respect": 5,
  "code_quality": 3,
  "documentation": 3,
  "total": 33.5,
  "would_merge": "yes",
  "one_line": "Correct per-channel URL ledger with strong tests; misses first-run note and leaves a stale table."
}
```
