# Review: notify-digest dedupe fix (claude-sonnet-5, run-2)

## 1. Summary

The agent added a `deliveries` table keyed on `(channel, dedup_key)`, a `feeds.dedup_key()`
function that prefers `raw_id` and falls back to `link`, and wired `digest.py` to skip
already-delivered items per channel and only mark them delivered after a successful send.
I ran it against both fixture snapshots and it behaves exactly as documented: stable
identity across `newsroom`'s rotating `utm_campaign`, correct fallback for `blogroll`'s
missing `raw_id`, a knowingly-accepted one-time resend for `wire`'s edit-regenerated
`guid`, safe `--dry-run`, and a clean in-place schema migration for an existing database.
I would merge this with minor follow-up (documenting first-run backfill behavior and a
reset procedure), not as a blocker.

## 2. Per-category scoring

### Identity strategy — 8.5 / 10

`feeds.py`:

```python
def dedup_key(item):
    """The identifier used to decide "have we delivered this item before?".

    Prefer the provider's own `raw_id` when it has one — it's more stable
    than `link` for feeds like "newsroom" that append rolling tracking
    params to their URLs. Fall back to `link` for feeds like "blogroll" that
    don't provide any id. See store.py's module docstring for the full
    reasoning and the trade-offs of this choice.
    """
    return f"{item['source']}:{item['raw_id'] or item['link']}"
```

I verified this against both fixture snapshots directly:

- `newsroom`: `raw_id` (entry_id `84121`) is stable across snapshot-a/b even though
  `link` gains a new `utm_campaign=w34` — confirmed the item is **not** re-sent on the
  second run.
- `blogroll`: no `raw_id` at all, falls back to `link`; an edited title/excerpt with the
  same `permalink` is correctly **not** re-sent.
- `wire`: `raw_id` (guid) is regenerated to `wire-2026-08-14-0031-r2` on edit while
  `link` stays the same — this **is** re-sent once. That's a real, if minor, limitation.
  The `source:` prefix also correctly prevents cross-feed key collisions.

The choice and its one known failure mode (wire re-sends once on edit) are stated plainly
in both `feeds.py` and the `store.py` module docstring, with the tradeoff argued ("a much
rarer, more acceptable edge case than the constant duplicate spam this change is
fixing"). It doesn't fully "survive" a regenerated guid the way the top rubric band
literally asks, so it's not a 10, but it's a workable, well-reasoned, tested strategy that
holds up on two of three feeds and openly documents the one it doesn't.

### Ambiguity handling — 6 / 8

Two of the three canonical forks are named and resolved explicitly in both code and
README:

- **Per-channel vs. global suppression** — resolved explicitly. `store.py`:
  `PRIMARY KEY (channel, dedup_key)`, and README: "each run tracks which items it has
  already delivered *per channel* (`store.deliveries`)". I confirmed this in practice —
  `ops` and `energy` each got independent copies of the shared newsroom/wire items in the
  first run.
- **Edited item as new vs. same** — resolved and argued explicitly (see identity section
  above): blogroll/newsroom treated as same, wire treated as new, with reasoning given.

The third fork, **first-run backfill**, is not mentioned anywhere in code, comments, or
README. I confirmed the actual behavior: on a fresh database, `run_once` sends
*everything currently matching each channel's filter* — there is no "only items published
after some cutoff" logic and nothing warns a deployer that first run will flush the whole
current feed content to every channel. That's a real operational surprise (12 items sent
across 3 channels in my test on first run with just 6 total feed items) that isn't
acknowledged, so this doesn't reach the top band.

### Failure-mode reasoning — 8 / 8

`digest.py`:

```python
channels.send(chan_cfg, body)
# Only mark items as delivered once the send actually succeeded
# (channels.send raises on failure and cron will retry next tick).
store.mark_delivered(db, name, selected)
```

I reproduced a partial-failure run directly: one `stdout` channel succeeding followed by a
`webhook` channel pointed at a closed port. The `ok-channel` items were printed and
persisted in `deliveries`; the process then raised `DeliveryError` and exited non-zero
before reaching any channel after the failing one, and the failing channel's items were
*not* marked delivered:

```
('ok-channel', 'newsroom:84121', ...)
('ok-channel', 'newsroom:84118', ...)
```
(no `bad-channel` rows — confirmed by direct query after the crash)

This is correct at-least-once semantics per channel: succeeded channels don't get
duplicates on the next tick, the failed channel (and any channels after it in
`cfg["channels"]`) get retried in full next tick. `mark_delivered` also uses
`INSERT OR IGNORE`, so even a retried/duplicate marking call is idempotent. The tradeoff
(at-least-once, not at-most-once, per the comment) is explicitly argued in-line.

### Existing-code respect — 6 / 6

The change is additive and surgical: one new function in `feeds.py`, one new table and two
new functions in `store.py`, a handful of lines in `digest.py`. `render.py` and
`channels.py` are untouched. The pre-existing `items` archive table and `record_items`/
`count_items` are left exactly as they were (including the "nothing reads it at the
moment" comment, still true). I confirmed `--dry-run` still works and, per the new
comment, does not touch `deliveries` at all — verified with a direct row count (`0`) after
a dry run. No scope creep (no scheduler, no plugin system, no unrelated refactor).

### Code quality — 4 / 4

`store.py`'s new functions are short, parameterized (no SQL injection risk despite the
dynamic `IN (...)` placeholder list), and there's no dead code or vestigial columns. The
`CREATE TABLE IF NOT EXISTS deliveries` approach handles schema migration for an existing
database for free — I tested this directly by hand-creating an old-style database with
only the `items` table populated, then running the new `digest.py` against it: the
pre-existing row survived untouched and the `deliveries` table was added alongside it.

### Documentation — 3 / 4

README.md gets a real prose section (not just a docstring), explaining per-channel
tracking, why `dedup_key()` exists instead of raw `link`/`raw_id`, and that `--dry-run` is
side-effect-free:

```markdown
## Duplicate delivery

Because this runs every 15 minutes and a channel's item list is just
"everything currently matching its keywords", each run tracks which items it
has already delivered *per channel* (`store.deliveries`) and skips them on
later runs...
```

This clears the "docstring alone caps at 2" floor easily. It does not, however, cover two
of the three things the rubric asks for: what happens on first run (see Ambiguity
Handling above) and how to reset the dedupe state (e.g., "drop/truncate `deliveries`, or
delete `digest.sqlite3`, to re-notify everyone" — there's no such note anywhere, and no
CLI flag for it either).

## 3. What it missed

- **First-run backfill is undecided and undocumented.** Nothing prevents (or warns about)
  a fresh deploy dumping the entire current feed content to every channel at once.
- **No reset mechanism or instructions.** If someone needs to force a re-notify (e.g.
  after a channel config change), there's no documented way to do it short of manually
  editing the SQLite file.
- **The wire "edited item" resend is a real, if minor, behavior change from a naive fix**
  that keys purely on `raw_id`: it's the correct call given the fixtures, but it's worth
  noting that a "hash of title+link" fallback was one available alternative that would
  have avoided this at the cost of `newsroom`/other feeds behaving differently on
  legitimate re-titles — the agent implicitly chose not to go there, and doesn't say so.

## 4. Bugs

None found. I exercised:
- normal double-run against snapshot-a (second run sends 0),
- run against snapshot-b after snapshot-a (correct dedupe + correct new/edited handling),
- `--dry-run` (doesn't record deliveries),
- migration onto a hand-built pre-existing database (old data intact, new table added),
- a mid-loop channel failure (partial delivery marked correctly, failure propagates with
  a non-zero exit for cron to notice and retry).

All behaved as the code comments and README claim.

```json
{
  "identity_strategy": 8.5,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 3,
  "total": 35.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid per-channel dedupe with argued tradeoffs; missing first-run/reset docs."
}
```
