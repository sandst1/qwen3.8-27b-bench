# Review: notify-digest dedup fix

## 1. Summary

The agent added a `sent` table keyed on `(dedup_key, channel)`, where `dedup_key`
is `source + canonical-link-with-tracking-params-stripped`, and only marks an
item sent *after* a channel's delivery call returns successfully. It touched
exactly three files (`digest.py`, `store.py`, `README.md`), left `feeds.py`,
`channels.py`, `render.py` untouched, and documented the dedup behaviour, the
identity choice, and the per-channel failure semantics in both docstrings and
the README. I would merge this: I reproduced the no-more-duplicates behaviour
against both fixture snapshots, confirmed `--dry-run` still never marks
anything sent, and confirmed a mid-batch channel failure leaves the succeeded
channel marked and the failed one retried — the only real gap is that the
README never says how to force a resend (delete the `sent` table / point
`--db` elsewhere), which is a small, fixable documentation hole, not a design
problem.

## 2. Per-category scoring

### Identity strategy — 9 / 10

`store.py` keys on `(source, canonical link)`, deliberately *not* on each
feed's native id:

```python
# store.py:62-83
def dedup_key(item):
    """A stable identity for an item, used to decide whether it was delivered.

    We key on (source, canonical-link) rather than the provider's own id,
    because the three providers cannot agree on one and no single field is
    stable across all of them:

      * newsroom has a stable `entry_id`, but rewrites its URL's tracking
        params on every pull — so keying on the raw URL would resend it.
      * wire keeps a stable URL but regenerates its `guid` on every edit — so
        keying on the id would resend an edited article.
      * blogroll has no id at all — the URL is all we have.

    The article's canonical URL (tracking params dropped) is the one reference
    that stays put in all three cases, so that is what we key on. `source` is
    prepended so two feeds that quote the same URL are treated as distinct.

    If a provider ever changes an item's *path* when it edits it, that item
    will look new and be sent once more — the acceptable price for a key that
    does not depend on any one provider's id scheme.
    """
    return f"{item['source']}\x1f{_canonical_link(item)}"
```

and strips exactly the tracking-param family the fixtures rotate:

```python
# store.py:46-49, 52-59
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "gbraid", "wbraid", "fbclid",
}

def _canonical_link(item):
    parts = urlsplit(item["link"])
    if not parts.query:
        return item["link"]
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in _TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))
```

I ran this against both fixture snapshots (`snapshot-a` then `snapshot-b`,
copied the tree to `/tmp` and ran `python3 digest.py` twice, swapping the feed
URLs in the config to point at `snapshot-b`):

- `newsroom`'s `port-fees` item rotates `utm_campaign=w33` → `w34` between
  snapshots; `entry_id` 84121 stays the same. Dedup correctly did **not**
  resend it — verified by diffing `sent` table contents and by the second
  run's `sent 2 items` (only the genuinely new `port-fees-union` entry went
  out).
- `wire`'s `guid` changes from `wire-2026-08-14-0031` to
  `wire-2026-08-14-0031-r2` between snapshots (an edit — the `description`
  also changed) while the link stays put. Not resent, as designed.
- `blogroll` has no id in either snapshot; its only edited item (title gets
  "(updated)" appended) is identified by link alone, correctly not resent.

All three fixture-specific failure modes named in the rubric — rotating
`utm_campaign`, missing `raw_id`, regenerated `guid` — are survived, and the
docstring states them explicitly. The reason this isn't a 10: the strategy is
a single uniform policy across all three feeds rather than a genuine per-feed
fallback chain, and it throws away the one strictly-stable signal available
(`newsroom`'s `entry_id`) in favour of simplicity. The agent *names* the
resulting gap ("if a provider ever changes an item's path... sent once more")
but doesn't close it even though closing it (prefer `raw_id` when present and
stable, else canonical link) would have been a few more lines and have been
strictly more robust for the one feed that actually has a good id.

### Ambiguity handling — 8 / 8

All three forks the rubric names are identified and resolved explicitly, not
just implemented correctly and left for the reader to infer:

1. **Per-channel vs. global suppression.** Resolved per-channel, stated in
   the README:

   ```markdown
   Each run remembers which items it has already delivered to each channel, in the
   `sent` table, so a channel does not get the same item on every 15-minute tick.
   ```

   and enforced by the schema (`PRIMARY KEY (dedup_key, channel)`,
   `store.py:34-39`) plus `seen_keys(conn, channel)` being scoped to one
   channel at a time (`store.py:110-113`).

2. **Edited item as new vs. same.** Named directly in the `dedup_key`
   docstring quoted above: keying on `wire`'s `guid` "would resend an edited
   article" — so the explicit design choice is that an edit which doesn't
   change the link is the *same* item, not a new one.

3. **First-run / newly-added-channel backfill.** Named in both the schema
   comment and the README:

   ```python
   # store.py:30-33
   # (dedup_key, channel) -> we have already delivered this item here.
   # PRIMARY KEY makes re-delivery impossible and is safe to re-run: a fresh DB
   # simply starts with an empty table, so a newly added channel sees the current
   # batch once and then only new items from then on.
   ```

   ```markdown
   A channel added later sees the current matching batch once and then only new
   items from then on.
   ```

   The same mechanism applies to a brand-new deployment's very first run
   (empty `sent` table ⇒ one full flood, then steady state) — the behaviour is
   correct and the write-up covers it, if not quite using the words "first
   run."

The chosen answer to #1 is also the *correct* one per the rubric's own note
that global suppression is the typical wrong answer — the agent avoided that
trap and said why.

### Failure-mode reasoning — 8 / 8

The marker is written per-channel, strictly after delivery, and the ordering
is deliberate:

```python
# digest.py:45-64
sent = 0
for chan_cfg in cfg["channels"]:
    name = chan_cfg["name"]
    seen = store.seen_keys(db, name)
    selected = [
        i for i in all_items
        if matches(i, chan_cfg) and store.dedup_key(i) not in seen
    ]
    if not selected:
        continue
    body = render.digest(selected, chan_cfg)
    if dry_run:
        print(f"--- would send to {name} ---")
        print(body)
        continue
    channels.send(chan_cfg, body)
    sent += len(selected)
    store.mark_sent(db, name, [store.dedup_key(i) for i in selected])
```

```python
# store.py:116-127
def mark_sent(conn, channel, keys):
    """Record that `keys` were delivered to `channel` on this run.

    Call this *after* the channel has actually accepted the digest, never
    before: if delivery fails the exception propagates to cron for a retry, and
    these keys stay unmarked so the same items go out again next tick.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO sent (dedup_key, channel) VALUES (?, ?)",
        [(k, channel) for k in keys],
    )
    conn.commit()
```

I verified the partial-failure case directly: two channels, `ops` (stdout,
succeeds) and `broken` (webhook pointed at a closed port). First run: `ops`
gets its digest and is committed to `sent`; `channels.send` then raises for
`broken`, the exception propagates out of `run_once`/`main`, and the process
exits non-zero (as cron expects for a retry). Second run against the same
feed data: `ops` prints nothing (already marked, correctly not resent) and
`broken` fails again (correctly retried, since it was never marked). This is
exactly "per-channel marking, correct ordering, no whole-batch loss or
duplication on partial failure."

The at-least-once choice is also named, not just implemented:

```markdown
An item is marked delivered only *after* the channel has accepted it, so a
failed delivery is simply retried on the next tick rather than dropped.
```

That's the tradeoff statement the top band asks for: favour an occasional
duplicate (narrow window between a channel accepting the payload and the
`INSERT INTO sent` commit) over ever silently dropping an item.

### Existing-code respect — 6 / 6

```
 README.md | 15 +++++++++++
 digest.py | 10 ++++++--
 store.py  | 86 ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++---
 3 files changed, 106 insertions(+), 5 deletions(-)
```

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are
byte-for-byte untouched. `--dry-run` still short-circuits before
`channels.send`/`mark_sent` (`digest.py:56-59`) — verified by running
`--dry-run` twice against the same snapshot and confirming it prints the same
full digest both times, never silently marking anything sent. The pre-existing
`items` archive table and its unconditional insert
(`store.record_items`, unchanged) are kept exactly as before, and the
decision to leave it alone is now explained rather than just inherited:

```python
# store.py:1-11
"""SQLite persistence.

Two tables with two jobs:

* `items` is an append-only archive of everything we have ever seen, so we can
  answer "did this ever come through?" later. It is deliberately *not*
  de-duplicated — the same article lands here once per fetch — and delivery
  logic does not read it.
* `sent` records which items we have already delivered to which channel. This
  is what stops a channel getting the same item on every 15-minute tick.
"""
```

This is precisely the rubric's "reusing the archive is fine if the
unconditional insert is dealt with" — it's dealt with by naming it as
intentional, not silently carried over.

### Code quality — 4 / 4

`CREATE TABLE IF NOT EXISTS sent (...)` in the same `executescript` as the
pre-existing `items` table means an existing production `digest.sqlite3`
upgrades in place the next time `store.connect` runs — no migration script,
no manual step, nothing to break on deploy. SQL is parameterized throughout
(`seen_keys`, `mark_sent`), `INSERT OR IGNORE` makes `mark_sent` idempotent
against double-marking, and there's no dead code or new vestigial columns —
`raw_id` stays in `items` exactly as before, still write-only, which is
consistent with its pre-existing status, not a new problem. The only minor
stylistic nit (not a correctness issue) is using a `\x1f`-joined string as a
composite key (`store.py:83`) instead of two real columns — it works and is
commented, but a `(source, link)` pair of columns in the primary key would
have been slightly more conventional SQL.

### Documentation — 4 / 4 cap minus 1 for the missing reset instructions → **3 / 4**

The new "## De-duplication" section in the README covers the mechanism, the
identity choice and *why*, and the backfill-on-new-channel behaviour:

```markdown
## De-duplication

Each run remembers which items it has already delivered to each channel, in the
`sent` table, so a channel does not get the same item on every 15-minute tick.
An item is marked delivered only *after* the channel has accepted it, so a
failed delivery is simply retried on the next tick rather than dropped.

Items are identified by `(source, link)`, not by the provider's own id: the
three providers cannot agree on one (newsroom has a stable `entry_id`, blogroll
has none, wire regenerates its `guid` on every edit), and the article URL is
the only field that stays stable across all three. See `store.dedup_key`.

A channel added later sees the current matching batch once and then only new
items from then on.
```

This is well above "docstring alone" — it's in the README *and* mirrored in
docstrings, in agreement with each other. What's missing is any mention of
how to force a resend (e.g. "delete the `sent` table" / "run against a fresh
`--db` path") — an operator who needs to re-notify a channel after a mistake
has no documented path to do it, and has to go read `store.py`'s schema to
figure out it's safe to just `DELETE FROM sent WHERE channel = ...`.

## 3. What it missed

- **No reset/replay instructions.** Covered above — the single real
  documentation gap.
- **Table growth has no story.** `sent` is append-only forever; there is no
  retention policy, no mention of pruning old rows, and no note about it in
  either the README or a code comment. For a cron job running every 15
  minutes indefinitely this will grow without bound. Not a correctness bug
  within the rubric's current scope, but the kind of thing "the next person
  to touch it" will eventually hit.
- **Single global identity policy instead of per-feed.** Flagged above under
  identity strategy — `newsroom`'s `entry_id` is explicitly called out in the
  docstring as stable, then not used. A link-path rename on `newsroom` or
  `wire` will cause a resend even though a per-feed identity could have
  avoided it for `newsroom`.
- **No handling of two cron instances overlapping.** SQLite's default
  locking will serialize writers, but there's no `BEGIN IMMEDIATE` or similar,
  and nothing in the code or docs addresses what happens if a previous
  15-minute run is still in flight (e.g. a slow webhook) when cron fires
  again. Outside what the prompt asked for, but worth naming since the prompt
  explicitly mentions cron.
- **Upgrading an existing production DB** gets a free backfill flood on the
  first post-upgrade run (the new `sent` table starts empty, so every
  currently-matching item in every channel goes out once) — functionally
  identical to the documented "new channel" case, but not called out
  explicitly as applying to *upgrades*, which is the actual rollout path for
  this fix.

## 4. Bugs

None found. Specifically checked and did not reproduce:

- **Duplicate across repeated runs of the same snapshot:** ran
  `python3 digest.py --config config.toml --db digest.sqlite3` twice against
  unmodified `snapshot-a` — second run printed `sent 0 items`, no items
  re-rendered.
- **Duplicate across snapshot-a → snapshot-b transition:** second run (config
  repointed at `snapshot-b`) sent exactly the two genuinely-new items
  (`port-fees-union` on `newsroom`) to `ops` and `everything`, and nothing to
  `energy` (no new matching items) — no re-delivery of the edited `wire` or
  `blogroll` items, no re-delivery of the `utm_campaign`-rotated `newsroom`
  items.
- **`--dry-run` marking anything as sent:** ran `--dry-run` twice in a row
  against the same snapshot; both runs printed the identical full digest,
  confirming `mark_sent` is never called on the dry-run path
  (`digest.py:56-59`, the `continue` is before `channels.send`/`mark_sent`).
- **Partial multi-channel failure dropping or duplicating a whole batch:**
  reproduced a run with one working `stdout` channel and one `webhook`
  channel pointed at a closed TCP port. First run: `ops` digest printed and
  committed to `sent`, then the process raised `DeliveryError` for `broken`
  and exited non-zero. Second run (same data): `ops` printed nothing
  (correctly not resent), `broken` raised the same `DeliveryError` again
  (correctly retried, since it was never marked sent). No batch was lost or
  duplicated across the two channels.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 3,
  "total": 38,
  "would_merge": "yes",
  "one_line": "Correct, well-reasoned per-channel dedup; only misses reset docs and a per-feed id fallback."
}
```
