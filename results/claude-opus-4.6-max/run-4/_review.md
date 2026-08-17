# Review — notify-digest dedup fix

## 1. Summary

The agent added a per-channel `sent(channel, link)` table keyed on item `link`, checked before sending and written after successful delivery, plus a matching README section explaining the choice and how to reset it. The core direction is right and the code is small and clean, but the chosen identity key (`link`) is not resilient to the `utm_campaign` rotation present in the `newsroom` fixture, so the exact "same item over and over" bug survives for that feed — and the new `UNIQUE(source, link)` index will crash `store.connect()` on any real pre-existing database, since the old code recorded every fetched item on every 15-minute tick with no such constraint. I would merge this only after fixing the migration crash and stripping tracking params from the identity key; as shipped it re-introduces the reported bug on one of the three feeds and breaks on first contact with production data.

## 2. Per-category scoring

### Identity strategy — 6 / 10

`store.py` picks item `link` as the sole dedup key for all three feeds, explicitly reasoning about why `raw_id`/`guid` are unreliable:

```python
# store.py
"""
The `sent` table tracks which items have been delivered to which channel so
we never send the same item twice.  We use the item *link* as the dedup key
because it is the most stable identifier across all feed formats:

  - newsroom: has a stable entry_id, but the URL is equally stable.
  - blogroll: has no machine identifier at all; permalink is all we have.
  - wire/generic: the guid is regenerated on every edit (typo fixes, retitles)
    so it is useless for dedup; the link stays constant.
"""
```

That claim about newsroom — "the URL is equally stable" — is false, and the fixtures are built to show it. `fixtures/snapshot-a/newsroom.json` and `snapshot-b/newsroom.json` carry the *same* `entry_id` for the "port fees" and "offshore tender" stories, but the URL's `utm_campaign` query param rotates (`w33` → `w34`):

```
snapshot-a: https://newsroom.example/2026/08/port-fees?utm_source=feed&utm_campaign=w33
snapshot-b: https://newsroom.example/2026/08/port-fees?utm_source=feed&utm_campaign=w34
```

I ran it end-to-end: seed the db against snapshot-a (both stories get sent to `ops`/`energy`), then point the config at snapshot-b and rerun against the same db:

```
$ python3 digest.py --config config.example.toml --db test.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.example.toml --db test.sqlite3   # rerun, still snapshot-a
sent 0 items
$ sed -i '' 's/snapshot-a/snapshot-b/g' config.example.toml
$ python3 digest.py --config config.example.toml --db test.sqlite3   # snapshot-b, 15 min later
=== ops ===
Ops digest — 2 item(s)
* Union responds to port fee inquiry  [newsroom]      <- genuinely new
* Regulator opens inquiry into port fees  [newsroom]   <- ALREADY SENT in snapshot-a
=== energy ===
Energy digest — 1 item(s)
* Grid operator delays offshore tender  [newsroom]     <- ALREADY SENT in snapshot-a
sent 6 items
```

Both "Regulator opens inquiry into port fees" and "Grid operator delays offshore tender" are re-delivered to their channels a second time — the exact complaint in the prompt — because the dedup key changed even though `entry_id` did not. The `wire` feed (guid rotates, link stable) and `blogroll` feed (no id, permalink stable, even survives an edited excerpt) both work correctly under this scheme, so it's one feed out of three that breaks, and it breaks silently: nothing in the code or README flags that newsroom URLs carry tracking params, and the docstring actively asserts the opposite. This matches the rubric's 6–8 band ("a single strategy that works for most items; the feed it breaks on is unaddressed or unnoticed") rather than the top band, which requires surviving rotating `utm_campaign` explicitly — the one thing this fixture set was built to test.

### Ambiguity handling — 5 / 8

Three forks exist; one is named and resolved, two are decided silently:

- **Per-channel vs. global suppression** — named and resolved correctly. The `sent` table's primary key is `(channel, link)` (store.py, schema block), and README says explicitly: *"The `sent` table in the SQLite database records `(channel, link)` pairs."* Full credit for this fork.
- **Edited item as new vs. same** — decided silently by the choice of `link` as key: `blogroll/snapshot-b`'s "Notes on port fee arithmetic (updated)" has the same `permalink` as snapshot-a but a changed title/excerpt, and it is correctly *not* re-sent. But this is a side effect of the key choice, never named as a decision anywhere in the code or docs — no comment or README line says "edited items at the same URL are treated as the same item and will not re-notify."
- **First-run backfill** — not discussed at all. On a fresh `sent` table, every item currently in every feed goes out immediately across all channels on the first tick, as I observed above (`sent 12 items` on the very first run). Whether that's desired (versus fetching the current feed to seed a `first_seen` baseline once, silently) is never argued either way.

One of three forks is named; the other two happen to land on defensible outcomes but are entirely unstated — solidly mid-band.

### Failure-mode reasoning — 5 / 8

Marking is per-channel and ordered correctly relative to delivery — `mark_sent` is only called after `channels.send` returns without raising:

```python
# digest.py
channels.send(chan_cfg, body)
store.mark_sent(db, chan_cfg["name"], [i["link"] for i in selected])
sent += len(selected)
```

If `channels.send` raises `DeliveryError` (see `channels.py`, webhook branch), `mark_sent` never runs for that channel, so a failed delivery is retried on the next tick rather than silently marked sent — correct at-least-once semantics, and it composes correctly with the per-channel loop (channel A succeeding and being marked doesn't cause channel B's failure to lose or duplicate anything). This is the right choice and I could reproduce it by hand (raising inside `channels.send` and confirming `sent` stays empty for that channel).

What's missing: there is no `try`/`except` around `channels.send` inside the `for chan_cfg in cfg["channels"]` loop, so an exception in one channel's delivery propagates and aborts `run_once` entirely — any channels later in the config list simply don't run that tick, silently, until the failing channel starts working again. That's a real interaction the agent didn't consider or mention (a permanently broken webhook config would starve every channel after it in the list, forever, without documentation of the caveat). Nor is there anywhere any explicit note that at-least-once (rather than at-most-once) was chosen and why. Per-channel marking and sane ordering are done; the tradeoff is never argued in the code or README, and one interaction (mid-loop failure blocking later channels) is unaddressed.

### Existing-code respect — 5.5 / 6

The agent touched only `digest.py` and `store.py`, left `feeds.py`, `channels.py`, `render.py` untouched, and dealt correctly with the fact that `items` is inserted unconditionally every tick — by adding a matching unique index and switching the insert to `INSERT OR IGNORE`:

```python
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_link ON items(source, link);
...
def record_items(conn, source, items):
    conn.executemany(
        "INSERT OR IGNORE INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)", ...
```

`--dry-run` was preserved and verified working (prints preview, never calls `mark_sent`, safe to run repeatedly). The archive semantics ("did this ever come through") are kept intact. Small, surgical diff overall — this is close to ideal minimalism, docked half a point only because the new unique index (see Bugs below) interacts badly with the *existing* unconditional-insert history that's already on disk in any real deployment, which is squarely "working with the codebase as it actually exists in production," not just as it exists in a fresh fixture run.

### Code quality — 1.5 / 4

The new code itself (`unsent_links`, `mark_sent`) is small, readable, uses parameterized SQL, and has no dead code. But the rubric explicitly calls out "schema migration handled for an existing database," and this is not handled — it's actively broken. I simulated a realistic pre-existing production DB (the kind that would exist after this codebase really has been "running from cron every 15 minutes," per the prompt) by inserting the same `(source, link)` three times under the *original* schema (no unique constraint existed before this patch, and the original `record_items` inserted unconditionally on every tick):

```
$ python3 -c "
import store
db = store.connect('/tmp/prod_sim.sqlite3')
"
FAILED: IntegrityError UNIQUE constraint failed: items.source, items.link
```

`store.connect()` runs `conn.executescript(SCHEMA)` unconditionally (store.py), and `CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_link ON items(source, link)` fails outright the moment it's applied to a table that already contains duplicate `(source, link)` rows — which is the normal, expected state of any database this code has actually been running against, since the pre-patch code recorded every item on every single poll with no deduplication at all. Deploying this patch over the existing `digest.sqlite3` used in production crashes the very first cron tick after rollout, with no migration path, no `try`/`except`, and no mention of the risk anywhere. This is a textbook version of exactly the failure mode the rubric names, so it caps this category low regardless of the otherwise-clean code.

### Documentation — 2.5 / 4

The README gets a real, substantive "Deduplication" section (not just a docstring), explaining the mechanism, the identity-key rationale (with a comparison table across the three feed formats), and how to force a re-send:

```markdown
## Deduplication
...
**Consequence**: if you need to re-send an item (e.g. after a bad render),
delete its row from the `sent` table:
DELETE FROM sent WHERE channel = 'ops' AND link = 'https://...';
The next cron tick will re-deliver it.
```

Two of the three things the rubric asks for are covered (dedupe behavior, how to reset). Missing: what happens on first run (no mention that a fresh `sent` table means the entire current feed backlog goes out on tick one), and no discussion of crash/retry semantics. Worse, the one clearly-stated technical claim in the doc — "The item `link` (permalink/URL) is stable across all three providers" — is demonstrably false for `newsroom` per the fixtures (see Identity strategy above), so the next engineer reading this doc will be actively misled into thinking the tracking-parameter case was considered and ruled out, rather than missed.

## 3. What it missed

- **utm_campaign / tracking-parameter normalization** — never considered; this is precisely what makes the newsroom feed re-duplicate (see Bugs).
- **First-run backfill** — undecided and undocumented; first tick against an empty `sent` table blasts every currently-live item to every matching channel.
- **Schema migration for existing databases** — no plan at all; the new unique index will crash on first contact with any DB that predates the patch.
- **Cross-channel failure isolation** — one channel raising inside the `for chan_cfg in cfg["channels"]` loop silently prevents every channel listed after it from running that tick, with no `try`/`except` and no note of the risk.
- **"Edited item" semantics** — the choice to treat same-URL/different-content as the same item is real (and arguably correct) but never named as a decision anywhere.

## 4. Bugs

**Bug 1 — duplicate delivery survives on the newsroom feed.** Reproduction above under Identity strategy: run against `snapshot-a`, then against `snapshot-b`, using the same db — "Regulator opens inquiry into port fees" and "Grid operator delays offshore tender" are sent a second time because their newsroom URLs carry a rotating `utm_campaign` param, even though `entry_id` is unchanged. This is the exact symptom named in the task prompt, unfixed for one of the three feeds.

**Bug 2 — `store.connect()` crashes on any pre-existing database.**
```
$ python3 -c "
import sqlite3
conn = sqlite3.connect('/tmp/prod_sim.sqlite3')
conn.execute('CREATE TABLE items (id INTEGER PRIMARY KEY, source TEXT, link TEXT)')
for _ in range(3):
    conn.execute(\"INSERT INTO items (source, link) VALUES ('newsroom','https://x/y')\")
conn.commit()
"
$ python3 -c "import store; store.connect('/tmp/prod_sim.sqlite3')"
sqlite3.IntegrityError: UNIQUE constraint failed: items.source, items.link
```
Since the pre-patch `record_items` inserted every fetched item on every 15-minute tick with no uniqueness constraint, any database that has actually been running this cron job — which is the stated situation in the prompt — will already contain duplicate `(source, link)` rows. Applying this patch in place crashes the digest run outright on the first tick after deployment, with no migration, catch, or warning anywhere.

```json
{
  "identity_strategy": 6,
  "ambiguity_handling": 5,
  "failure_mode_reasoning": 5,
  "existing_code_respect": 5.5,
  "code_quality": 1.5,
  "documentation": 2.5,
  "total": 25.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Right shape, but link-only key and index migration both crash on real data."
}
```
