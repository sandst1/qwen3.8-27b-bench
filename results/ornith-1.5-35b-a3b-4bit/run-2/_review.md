# Review

## 1. Summary

The agent added a `sent` table keyed on `(channel, source, canonical_link)` in `store.py`, and rewired `digest.py`'s send loop to only deliver items a channel hasn't already received, marking them sent only after a successful `channels.send()`. It correctly identified that a naive `raw_id`/`guid` dedupe would fail on all three fixture feeds and instead keyed identity on the URL with the query string stripped, which I verified against `fixtures/snapshot-a` → `snapshot-b` actually suppresses the rotated-`utm_campaign`, edited-title, and regenerated-`guid` cases while still delivering the one genuinely new item. I would merge this with fixes: the logic is sound and tested, but the README was never touched, so the dedupe behavior, reset procedure, and first-run backfill risk are undocumented anywhere a future operator would look.

## 2. Per-category scoring

### Identity strategy — 8 / 10

`store.canonical_link` (store.py:65-78) is the single strategy used for every feed:

```python
def canonical_link(link):
    """Stable identity for a feed item.

    The feeds disagree on identifiers: newsroom ids are stable, but the generic
    feed regenerates its guid on every edit and the blogroll has no id at all,
    so `raw_id` cannot be trusted. The one thing that survives an edit is the
    link, so we key on it. The query string is dropped because providers rewrite
    tracking params (utm_*, ...) on every pull, which would otherwise make a
    stable item look new.
    """
    if not link:
        return ""
    parts = urllib.parse.urlsplit(link)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
```

I ran the fixtures to check this against all three feeds' known gotchas:
- **newsroom**: `utm_campaign` rotates `w33` → `w34` on the same URL path; stripping the query fixes it (confirmed — `port-fees` item does not re-appear in the snapshot-b run).
- **blogroll**: no `raw_id` at all, and the "port fee arithmetic" post's title/excerpt change between snapshots but `permalink` doesn't; confirmed suppressed.
- **wire** (`format = "generic"`): `guid` goes from `wire-2026-08-14-0031` to `...-r2` on edit, but `link` is stable; confirmed suppressed.

This is a real, verified fix for every case the fixtures exercise, and the reasoning is stated in the docstring, not just implied. It's held back from the top band because it's one strategy applied uniformly rather than a per-feed/fallback chain — `newsroom`'s `entry_id` is actually stable and unused, so if a newsroom URL is ever regenerated on a retitle (plausible if slugs are headline-derived, which the fixture data doesn't rule out), there's no `raw_id` fallback to catch it. That residual failure mode isn't discussed anywhere.

### Ambiguity handling — 4 / 8

Three forks exist; one is named explicitly, one is decided silently but correctly, one isn't decided at all.

- **Per-channel vs global suppression** — named, in a comment right above the loop (digest.py:45-48):
  ```python
  # Dedup per channel: an item is delivered to a given channel at most once.
  # This matters most for the firehose channel (empty keywords = matches all);
  # global dedup would let the narrower channels claim every item and leave
  # the firehose empty.
  ```
  Confirmed correct: `test_firehose_is_not_starved` (test_digest.py:59-65) checks the firehose still gets all 6 items even though `ops`/`energy` also claim overlapping items.
- **Edited item as new vs same** — decided silently (by using link-only identity, an edit never counts as new), and it matches the fixture's implied intent, so this is the "silent but correct" case.
- **First-run backfill** — not addressed anywhere. The `sent` table starts empty regardless of how much history is already in `items`. On the very first run after this patch is deployed to the existing cron job, every item currently in every feed will be treated as new and blasted to every matching channel at once — this is exactly the kind of thing a person deploying this fix to production needs to be warned about, and there's no comment, README note, or CLI flag addressing it.

One fork named + one correct-but-silent + one real production risk left completely undecided/undocumented lands this in the low-to-mid band.

### Failure-mode reasoning — 6.5 / 8

Per-channel marking, correct ordering, with reasoning present in comments (though not surfaced as a single stated tradeoff). digest.py:60-67:

```python
    body = render.digest(fresh_items, chan_cfg)
    if dry_run:
        print(f"--- would send to {chan_cfg['name']} ---")
        print(body)
        continue
    channels.send(chan_cfg, body)
    store.mark_sent(db, chan_cfg["name"], fresh)
    sent += len(fresh)
```

`mark_sent` is only called after `channels.send` returns without raising, so a channel that throws mid-delivery leaves its items unmarked and they'll be retried on the next tick — matching the pre-existing `channels.py` contract ("best effort... cron will pick us up again"). The reasoning is documented in the function docstring, store.py:99-104:

```python
def mark_sent(conn, channel, rows):
    """Record that `rows` (source, link) have been delivered to `channel`.

    Call this only after delivery succeeds, so a failed send is retried next
    tick rather than silently skipped.
    """
```

and the `sent` table comment states the idempotency argument (store.py:29-30: "PRIMARY KEY makes INSERT OR IGNORE idempotent, so a retry never double-counts"). This is real per-channel at-least-once reasoning, verified: if channel 2 raises, the uncaught exception halts the run before channel 3 is attempted, so channel 3 gets no items that tick, but nothing is falsely marked sent. Docked half a point because the at-least-once choice is never named as such or weighed against the alternative (e.g., no discussion of what a channel-3-never-runs window means if the upstream feed's rolling window later drops the item before channel 3 catches up).

### Existing-code respect — 5.5 / 6

Surgical: `feeds.py`, `channels.py`, `render.py`, `config.example.toml` are untouched (`diff -rq` confirms only `digest.py` and `store.py` changed, plus a new `test_digest.py`). `--dry-run` still works and was verified not to write to the `sent` table:

```
$ python3 digest.py --config config.toml --db digest.sqlite3 --dry-run   # first run, dry
$ python3 digest.py --config config.toml --db digest.sqlite3            # real run right after
sent 12 items   # not 0 — dry-run correctly left no marks
```

The `items` archive table and its unconditional insert (store.py `record_items`) are left exactly as before; the agent didn't try to repurpose it for dedup (which would have needed the unconditional-insert problem solved) and instead added a dedicated `sent` table. That sidesteps the rubric's stated trap cleanly. Not a perfect score only because the pre-existing `items` table now duplicates a chunk of what `sent` tracks (both grow forever, `items` unindexed on link) with no comment tying them together or noting the redundancy for a future reader.

### Code quality — 3.5 / 4

Clean, no dead code, sensible SQL (composite primary key exploited via `INSERT OR IGNORE` for idempotency, store.py:31-37), schema migration is a non-issue since `CREATE TABLE IF NOT EXISTS` on an existing DB just adds the new table. The agent also added `test_digest.py`, which is real coverage, not just decoration, and it passes:

```
$ python3 -m unittest -v test_digest
test_firehose_is_not_starved ... ok
test_only_genuinely_new_items_are_sent ... ok
test_second_run_sends_nothing ... ok
```

Small deduction: no doc comment ties the new `sent` table to the pre-existing, now partially redundant `items` archive, and the module-level docstring in store.py doesn't mention `test_digest.py` needing to be run from the project root as a maintenance note anywhere outside the test file itself.

### Documentation — 2 / 4

`README.md` is byte-for-byte identical to the original — `diff` confirms no changes. Everything a future maintainer would need (why link-based identity, how to reset dedupe state, what a fresh deploy does to an existing feed backlog) lives only in code docstrings and one inline comment. Per the rubric, a docstring alone caps this category at 2, and that's exactly what happened here — there's no answer anywhere to "how do I reset this" (answer would be: delete rows from `sent`, or the whole DB) or "what happens on first run against a feed with months of history" (answer: it all goes out at once — see the ambiguity-handling gap above).

## 3. What it missed

- **First-run / redeploy backfill**: never decided, never mentioned. Deploying this to the existing cron job floods every channel with its entire current feed backlog on the next tick, once, silently.
- **README**: not touched at all, despite the task explicitly asking to leave the codebase so "the next person... understands what you chose and why." All the reasoning lives in code, not in the one place (README) a new operator is told to look (README.md line 4 in the original still just says "filters the items per channel, and sends each channel a digest" — no mention dedup exists).
- **No reset/ops story**: nothing tells a future operator how to force a resend (delete from `sent`) or how big the `items`/`sent` tables get over time.
- **Per-feed identity nuance**: newsroom's `entry_id` is stable and unused; no fallback chain, no note that link-based identity could break if a CMS ever changes a newsroom URL on retitle.
- Uncommitted: the working tree still has `digest.py`/`store.py` as unstaged modifications and `test_digest.py` untracked (`git status`), i.e., the agent never committed its own work, which is a process nit rather than a scoring category but worth flagging.

## 4. Bugs

None found. I exercised:
- `snapshot-a` twice back to back → second run sends 0 items (correct).
- `snapshot-a` then `snapshot-b` → only the genuinely new "Union responds to port fee inquiry" item is delivered; the utm-rotated, guid-regenerated, and title-edited items are all correctly suppressed (correct).
- `--dry-run` followed by a real run → dry-run does not mark anything sent (correct, and not a regression from the original flag's behavior).
- The bundled `test_digest.py` suite passes 3/3.

The one thing that will bite someone in production isn't a code bug but the undocumented first-run behavior described above.

```json
{
  "identity_strategy": 8,
  "ambiguity_handling": 4,
  "failure_mode_reasoning": 6.5,
  "existing_code_respect": 5.5,
  "code_quality": 3.5,
  "documentation": 2,
  "total": 29.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid tested per-channel link dedup; README untouched, first-run backfill risk undocumented."
}
```
