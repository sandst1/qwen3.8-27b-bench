# Review

## 1. Summary

The agent added a `sent` table keyed on `(channel, source, link)`, checked via `store.filter_unsent()` before rendering and written via `store.mark_sent()` after delivery, and documented the choice in docstrings in `digest.py` and `store.py`. The design gets per-channel suppression and crash-retry ordering right, but the chosen identity key — `link`, used uniformly across all three feeds — is provably wrong for the `newsroom` feed, whose links carry a rotating `utm_campaign` parameter; running the two fixture snapshots back-to-back reproduces a duplicate delivery for the exact scenario the fixtures were built to test. Compounding that, `--dry-run` now calls `mark_sent()`, so previewing a digest silently consumes the dedup state and the following real run sends nothing — a serious, easily-triggered regression of long-standing CLI behavior. I would not merge this as-is; the per-channel/mark-after-send skeleton is worth keeping, but the identity key and the dry-run side effect both need to be fixed first.

## 2. Per-category scoring

### Identity strategy — 4 / 10

The agent uses `link` as the sole identity key for every feed, argued in `store.py:17-28`:

```python
             * We use `link` as the stable identity key for all three feed
               formats:
                 - newsroom  has a stable raw_id (entry_id), but link is
                   equally stable and avoids a special-case branch.
                 - blogroll  has NO raw_id at all (None every time), so link
                   is the only viable key.
                 - wire/generic  has a guid that the provider regenerates on
                   every edit, making it useless for deduplication; link is
                   stable across edits.

             Using link uniformly keeps the logic simple and correct for all
             three providers.
```

The claim "link is equally stable" for `newsroom` is false, and the fixtures were clearly built to prove it: `fixtures/snapshot-a/newsroom.json` has entry `84121` at
`https://newsroom.example/2026/08/port-fees?utm_source=feed&utm_campaign=w33`, and `fixtures/snapshot-b/newsroom.json` has the **same** `entry_id: 84121` at
`...&utm_campaign=w34` — a different `link`. Running the two snapshots through the agent's own code confirms the regression:

```
=== RUN 1: snapshot-a (real send) ===
sent 12 items   # includes "Regulator opens inquiry into port fees" [newsroom, w33]

=== RUN 2: same snapshot-a again ===
sent 0 items    # correctly deduped

=== switch config to snapshot-b, RUN 3 ===
=== ops ===
Ops digest — 2 item(s)
* Union responds to port fee inquiry  [newsroom]      <- genuinely new
* Regulator opens inquiry into port fees  [newsroom]   <- SAME entry_id 84121, resent
sent 6 items
```

`entry_id 84121` is delivered twice to the `ops` channel with the campaign tag as the only difference — the literal bug the task was filed to fix, for the one feed (`newsroom`) that has a genuinely stable identifier (`feeds.py:47`, `raw_id: str(r["entry_id"])`) that the agent chose not to use. Meanwhile `blogroll` and `wire` dedupe correctly across the snapshots because their permalink/link happens to be stable there. This is a single strategy that works for 2 of 3 feeds and breaks, unnoticed, on the third — and the docstring's justification for using `link` on `newsroom` is factually wrong rather than a deliberately accepted tradeoff.

### Ambiguity handling — 5 / 8

- **Per-channel vs. global suppression** — named and resolved explicitly and correctly. `store.py:13-15` and the `sent` table's `(channel, source, link)` key back this up; verified working (an item can appear in one channel's digest without appearing in another's history).
- **Edited item as new vs. same** — named explicitly (`store.py:19-25`, `digest.py` docstring), and the decision ("same", via `link`) is stated. It's correct for `blogroll`/`wire` in the fixtures (title/summary edits don't reset a still-unread item), but it's really a side effect of the (buggy) identity choice rather than an independently reasoned fork.
- **First-run backfill** — not addressed anywhere. On a fresh `digest.sqlite3`, the very first invocation will deliver every item currently in every feed to every matching channel in one shot, with no comment, flag, or README note acknowledging this. For a feed that's been running unattended and is now being pointed at a real config for the first time, that's a real operational surprise this task should have named.

Two of three forks are named and one (backfill) is silent — mid-band, docked slightly because the "edited item" resolution is incidental to a broken identity choice rather than independently argued.

### Failure-mode reasoning — 5 / 8

Per-channel marking is correctly ordered relative to delivery:

```python
# digest.py
channels.send(chan_cfg, body)
# Mark after a successful send.  If send() raises, we don't mark, so
# the next run will retry — acceptable; better than silent data loss.
store.mark_sent(db, channel, selected)
```

If `channels.send()` raises for one channel, the whole `run_once` call propagates the exception (nothing catches `channels.DeliveryError` in the loop), so later channels in `cfg["channels"]` are simply retried on the next cron tick 15 minutes later — no double-send, no permanent loss, and the at-least-once tradeoff is stated in the comment above. That part is sound.

What's missing is any consideration of `--dry-run` as a failure mode in its own right. The agent decided dry-run should also call `mark_sent()`:

```python
if dry_run:
    print(f"--- would send to {channel} ---")
    print(body)
    # Record as sent even in dry-run so repeated --dry-run invocations
    # also show only new items, matching production behaviour.
    store.mark_sent(db, channel, selected)
    continue
```

This is a self-inflicted failure mode: a human running `--dry-run` to preview a digest against the real database permanently marks those items as sent, so the subsequent real run delivers nothing for them. Reproduced directly:

```
$ python3 digest.py --db test1.sqlite3 --dry-run   # previews 12 items across 3 channels
$ python3 digest.py --db test1.sqlite3             # real run, same db
sent 0 items
```

That's a batch silently lost from the perspective of anyone who previews before sending — exactly the kind of partial-failure window the rubric asks about, just self-inflicted rather than caused by a channel crash. No comment anywhere weighs this tradeoff or warns an operator against pointing `--dry-run` at the production db.

### Existing-code respect — 3 / 6

`feeds.py`, `channels.py`, and `render.py` are untouched; the new `sent` table is additive and doesn't disturb the existing `items` archive or its unconditional insert (which was pre-existing behavior, not something this task needed to fix). The CLI surface (`--config`, `--db`, `--dry-run`) is preserved.

However, `--dry-run`'s documented contract — "prints what would be sent instead of sending it" (README.md, unchanged) — is broken by the change above: dry-run now has a real, persistent side effect on the sent-state, which is exactly the kind of `--dry-run` regression the rubric singles out. The docstring in `digest.py` acknowledges the change ("Called after a successful delivery (**or after a dry-run preview**)") but nothing updates the still-present README claim that dry-run doesn't affect production state.

### Code quality — 3 / 4

The new functions are short, readable, and use parameterized queries throughout (no injection risk despite the dynamically-sized `VALUES (?,?),(?,?)...` clause in `store.py:90-95`). Minor blemishes: a stray trailing space (`store.py:91`, `flat_params = [v for pair in links for v in pair] `), and the misleading/incorrect comment about `newsroom`'s link stability (`store.py:19-20`) is a real code-quality defect, not just a documentation one — a future reader trusting that comment will not think to check `feeds.py`'s own note that `newsroom` carries a stable `entry_id`. No dead code or vestigial columns were introduced; schema addition uses `CREATE TABLE IF NOT EXISTS` / `CREATE UNIQUE INDEX IF NOT EXISTS`, so it applies cleanly to an existing database without migration ceremony.

### Documentation — 1 / 4

Docstrings in `store.py` and `digest.py` are thorough (arguably over-confident, given they contain the incorrect `newsroom` claim), but the rubric caps a docstring-only response at 2, and this one has an additional problem: `README.md` was not touched at all. It still reads:

```
`--dry-run` prints what would be sent instead of sending it.
```

which is no longer accurate — dry-run now mutates the dedup log. The README also doesn't mention the new `sent` table, how to reset deduplication (e.g., drop the table, delete the db, or that there's no reset mechanism at all), or what happens on first run against a fresh database. The file table (`README.md`) still describes `store.py` as "SQLite archive of everything seen," omitting the dedup role entirely.

## 3. What it missed

- **First-run behavior** — never named or documented; a fresh database will backfill-and-deliver everything currently in the feeds on the first tick.
- **Reset/replay** — no documented (or coded) way to say "resend this item" or "start dedup over"; an operator would have to know to manually touch the `sent` table.
- **`--dry-run` semantics** — the decision to make dry-run consume dedup state is made silently in a code comment and never surfaces in the README or CLI help; it's also the wrong default for a flag whose whole purpose is safe inspection.
- **Feed-specific identity** — despite `feeds.py`'s own comments distinguishing "stable raw_id" (newsroom) from "guid regenerates on edit" (wire) from "no id at all" (blogroll), the agent flattened all three into one strategy and got the one case with a genuinely reliable id wrong.
- **Unbounded growth of `sent`** — not addressed either way (fine to leave unaddressed, but also not named as an open question, e.g. no TTL/pruning discussed for a table that grows forever, one row per (channel, item) pair, forever).

## 4. Bugs

**Bug 1 — duplicate delivery survives the fix for `newsroom` items whose link changes (utm_campaign rotation).**
Repro (using the shipped fixtures against the agent's own code):
```
$ python3 digest.py --db test2.sqlite3                       # snapshot-a config
sent 12 items   # includes newsroom entry_id 84121, link ...&utm_campaign=w33

$ python3 digest.py --db test2.sqlite3                       # snapshot-a again
sent 0 items    # correct: deduped

$ sed -i '' 's/snapshot-a/snapshot-b/' config.toml
$ python3 digest.py --db test2.sqlite3                       # snapshot-b config
sent 6 items    # entry_id 84121 (same article!) resent, link now ...&utm_campaign=w34
```
This is the exact "same item over and over" complaint the task was filed for, reproduced for the `newsroom` feed specifically.

**Bug 2 — `--dry-run` permanently consumes dedup state, causing the next real run to send zero items.**
Repro:
```
$ python3 digest.py --db test1.sqlite3 --dry-run   # previews 3 channels, 12 total items
$ python3 digest.py --db test1.sqlite3             # real run against same db
sent 0 items
```
Anyone using `--dry-run` as documented ("prints what would be sent instead of sending it") to sanity-check a config change against the live database will silently lose that batch of notifications for real.

```json
{
  "identity_strategy": 4,
  "ambiguity_handling": 5,
  "failure_mode_reasoning": 5,
  "existing_code_respect": 3,
  "code_quality": 3,
  "documentation": 1,
  "total": 21,
  "would_merge": "no",
  "one_line": "Per-channel dedup skeleton is sound but link-only identity and dry-run both break in the fixtures."
}
```
