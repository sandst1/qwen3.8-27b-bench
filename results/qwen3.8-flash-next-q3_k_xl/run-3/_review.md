# Review — notify-digest dedupe fix

## 1. Summary

The agent added a `sent` table keyed by `(channel, item_key)`, where
`item_key` is built from a per-feed `identity` field chosen individually for
each of the three providers based on which raw field is actually stable
across the two fixture snapshots (`feeds.py:12–27, 59–102`); delivery is
filtered against `sent` before rendering and only recorded after
`channels.send()` succeeds (`digest.py:45–66`). I verified this against both
fixtures and a simulated mid-run crash and it behaves exactly as documented:
no re-sends on a second poll of the same snapshot, correct partial delivery
on snapshot-b (only the genuinely new newsroom item goes out, edited
items are correctly treated as not-new), and correct at-least-once recovery
when a channel fails mid-run. I would merge this after fixing one factual
error in the new README text (dry-run is documented as not touching the
database, but it does) and adding a line on how to reset the dedupe state.

## 2. Per-category scoring

### Identity strategy — 10 / 10

The agent picked a different identity field per provider, and the choice in
each case is exactly the field that survives the specific failure mode
present in that provider's fixture data — this is not a guess, the docstring
states it was checked against both snapshots:

```python
# feeds.py:12-23
`identity` is the value we dedupe deliveries on, and it is deliberately
*not* always the same underlying field, because no single field is stable
across all three providers (verified against fixtures/snapshot-a vs -b):

    newsroom  -> entry_id.  Stable, but the URL is not: it carries a
                 rotating utm_campaign parameter, so `link` would look
                 like a brand-new item every campaign.
    blogroll  -> permalink.  There is no id at all; the title and excerpt
                 do get edited, but the permalink never changes.
    wire      -> link.       The guid is regenerated whenever the provider
                 edits an item, so it would re-trigger delivery on typo
                 fixes; the link is stable.
```

Each branch of `fetch()` sets the field accordingly:

```python
# feeds.py:59-71 (newsroom)
return [
    {
        ...
        "raw_id": str(r["entry_id"]),
        "identity": str(r["entry_id"]),
    }
    for r in rows
]
```
```python
# feeds.py:74-87 (blogroll — raw_id is always None)
if fmt == "blogroll":
    # No id of any kind; the permalink is the only stable field.
    return [
        {
            ...
            "raw_id": None,
            "identity": r["permalink"],
        }
        for r in doc.get("posts", [])
    ]
```
```python
# feeds.py:91-102 (generic/wire — guid rotates on edit)
return [
    {
        ...
        "raw_id": r.get("guid"),
        "identity": r["link"],
    }
    for r in doc.get("items", [])
]
```

I ran both snapshots through it (see §Bugs for exact commands). Snapshot-a →
snapshot-b: the newsroom item whose `entry_id` didn't change (84121) was
correctly *not* resent despite its `utm_campaign` flipping from `w33` to
`w34`; the wire item whose `guid` gained a `-r2` suffix on edit was correctly
not resent because `link` is unchanged; the blogroll post retitled `(updated)`
was correctly not resent because `permalink` is unchanged. This is exactly
the three failure modes the rubric's top band names — rotating
`utm_campaign`, missing `raw_id`, regenerated `guid` — and the choice is
stated with its failure modes in the docstring above, not just implemented.

### Ambiguity handling — 8 / 8

All three named forks are surfaced and resolved, not just picked silently:

- **Per-channel vs global suppression** — resolved as per-channel, stated in
  the schema itself: `PRIMARY KEY (channel, item_key)` (`store.py:41`), and
  in the README table description.
- **Edited item as new vs same** — resolved as *same* (no resend on edit),
  and the reasoning is given per-provider in the `feeds.py` docstring quoted
  above ("it would re-trigger delivery on typo fixes", "the title and excerpt
  do get edited, but the permalink never changes").
- **First-run backfill** — named and the default defended explicitly:

  ```markdown
  # README.md:57-58
  * **First run on a fresh DB floods.** Everything looks new. That is usually
    what you want; if not, seed the DB with `--dry-run`-style logic by hand.
  ```

  I confirmed this: a fresh DB against snapshot-a sends all 12
  channel-deliveries on the first run, 0 on an immediate second run.

A fourth edge case not asked for by the rubric's three named forks, but
relevant to the same "silently vs explicitly" scoring dimension, is also
named and defended:

```markdown
# README.md:59-60
* **Renaming a channel re-sends its history**, since `sent` rows are keyed by
  channel name. Rename rarely, or migrate the `sent` table when you do.
```

I verified this by renaming `ops` → `ops2` in the config against a
populated DB: all 3 previously-delivered items were sent again under the new
name, exactly as documented.

### Failure-mode reasoning — 7.5 / 8

Marking is per-channel and happens strictly after a successful send:

```python
# digest.py:63-66
if dry_run:
    ...
    continue
channels.send(chan_cfg, body)
store.mark_sent(db, chan_cfg["name"], {feeds.item_key(i) for i in selected})
sent += len(selected)
```

```python
# store.py:76-86
def mark_sent(conn, channel, keys):
    """Record that these item keys were delivered to this channel.

    Call this only after channels.send() returned successfully; that way a
    failed delivery leaves no trace and the items are retried next tick.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, item_key) VALUES (?, ?)",
        [(channel, k) for k in keys],
    )
    conn.commit()
```

The tradeoff (favor redelivery over silent loss — i.e. at-least-once) is
argued in three separate places (`digest.py:48-49`, `store.py:11-15`,
`README.md:39-40`), all with the same wording: a failed delivery "leaves no
trace" and "is retried on the next tick."

I reproduced the exact scenario the rubric asks about — channel two failing
after channel one succeeded — with a `webhook` channel pointed at a closed
port between two `stdout` channels. Result: channel one's items were
rendered and marked sent (row present in `sent` afterward); the broken
channel raised and crashed the whole process (uncaught — this is inherited,
unchanged `channels.py` behavior: "we let the exception propagate and cron
will pick us up again"); the third channel, `everything`, was never reached
in that tick. On the next invocation, channel one correctly sent nothing
(already marked), and `everything` correctly received its full backlog. This
is the right outcome for the stated guarantee, and it holds together with
`store.mark_sent`'s per-channel, post-send-only commit.

Docked half a point because the reasoning, while present and correct, never
explicitly walks through the two-channel crash scenario in prose the way the
rubric's failure-mode description does — you have to derive it from the
`INSERT OR IGNORE` per-channel commit and the general "retried next tick"
comment rather than being told directly "channel 1 stays marked, channel 3
is retried whole." The mechanism earns the credit; the write-up doesn't
quite spell out the specific scenario.

### Existing-code respect — 6 / 6

`channels.py`, `render.py`, `config.example.toml`, and the fixtures are
byte-identical to the original (`diff` confirms no changes). `feeds.py` was
extended additively — every existing branch and field is untouched, only
`identity` and the new `item_key()` helper were added. The `items` archive's
unconditional insert (called out explicitly in the rubric) is kept exactly
as before, and the new store.py docstring is honest that it isn't being
dealt with, just decoupled from the actual delivery-dedupe fix:

```python
# store.py:5-8
`items`   an archive of everything we have ever fetched, mostly so that we
          can answer "did this ever come through?" when someone asks. It is
          append-only and may contain the same item many times (once per
          poll that saw it); nothing dedupes it.
```

The new `sent` table is added via `CREATE TABLE IF NOT EXISTS` inside the
same `executescript` used for `items`, so it's a safe additive migration —
I tested this by hand-building a pre-existing DB with only the old `items`
table/index and running the new `digest.py` against it; it added `sent`
without incident and delivered normally.

### Code quality — 4 / 4

The diff is small and targeted: a 5-line dict addition and a
15-line `item_key()` helper in `feeds.py`; a table, `seen_keys()`, and
`mark_sent()` in `store.py`; ~10 changed lines in `digest.py`. Naming
matches the existing module's style (`record_items`/`count_items` next to
the new `seen_keys`/`mark_sent`). SQL is parameterized throughout, uses a
sensible composite primary key, and `INSERT OR IGNORE` makes `mark_sent`
naturally idempotent against retries. No dead code, no vestigial schema.

One very minor nit (not scored down): `feeds.item_key(i)` is computed twice
for each selected item per channel — once in the filter comprehension
(`digest.py:55`) and again when building the set passed to `mark_sent`
(`digest.py:65`) — trivial at this data volume but a small avoidable
recomputation.

### Documentation — 3 / 4

The README goes well past a docstring: it added a dedicated section naming
the mechanism, the identity-per-provider table with the *why* for each row,
and an explicit "known sharp edges" section covering first-run flood and
channel-rename resend (both verified above to be accurate). This alone would
be top-of-band material. Docked one point for two real gaps:

1. **The README makes a false claim about `--dry-run`:**

   ```markdown
   # README.md:13-14
   `--dry-run` prints what would be sent instead of sending it; it does not
   touch the database.
   ```

   This is not true — see Bugs below. `store.record_items()` runs
   unconditionally before the dry-run check and writes into `items` every
   time, dry-run or not.

2. **No instructions for resetting the dedupe state.** The rubric asks
   specifically whether the README explains "how to reset it"; there is no
   mention anywhere of deleting rows from `sent`, dropping the DB, or any
   other reset path (`grep -i reset` across the repo returns nothing).

## 3. What it missed

- **How to reset dedupe** is not documented (see above) — a real person
  hitting "we need to resend last week's digest" has no guidance.
- **`sent` table retention.** Every delivered item stays in `sent` forever;
  there's no discussion of pruning it, unlike the explicit acknowledgment
  given to the unbounded `items` archive. Over a long enough deployment this
  is the same unbounded-growth question the rubric flags for `items`, just
  not raised for the new table.
- **Concurrent/overlapping cron runs.** If a run takes longer than 15
  minutes (e.g. a slow webhook), a second `digest.py` invocation could start
  while the first is still executing. `seen_keys()` reads and `mark_sent()`
  writes are two separate statements with no locking between them, so two
  overlapping processes could both read the same "not yet seen" state and
  both deliver the same item. This isn't discussed anywhere, and it's a
  more realistic failure mode for a cron job than most of what is covered.
- **No automated test** was added despite the fixtures being purpose-built
  for exactly this regression; the agent verified manually (visible in the
  session, not the diff) but leaves nothing for the "next person" to run.
- **First-run backfill's suggested workaround is hand-wavy.** "Seed the DB
  with `--dry-run`-style logic by hand" names the fork but doesn't give an
  actual seeding path — reasonable to leave unbuilt, but the suggestion
  itself doesn't quite work as written, since dry-run doesn't write to
  `sent` at all (see Bugs).

## 4. Bugs

**Dry-run writes to the database despite the README's claim that it doesn't.**

`digest.py` calls `store.record_items()` unconditionally, before the
per-channel/dry-run branch:

```python
# digest.py:32-41
def run_once(cfg, db, dry_run=False):
    all_items = []
    for feed_cfg in cfg["feeds"]:
        try:
            items = feeds.fetch(feed_cfg)
        except feeds.FeedError as exc:
            print(f"warn: feed {feed_cfg['name']} failed: {exc}", file=sys.stderr)
            continue
        store.record_items(db, feed_cfg["name"], items)   # <- runs even with --dry-run
        all_items.extend(items)
```

Reproduction (in a scratch copy under `/tmp`, not the reviewed directory):

```sh
$ cp config.example.toml config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3 --dry-run >/dev/null
$ sqlite3 digest.sqlite3 "select count(*) from items;"
6
$ python3 digest.py --config config.toml --db digest.sqlite3 --dry-run >/dev/null
$ sqlite3 digest.sqlite3 "select count(*) from items;"
12
```

Every `--dry-run` invocation grows `items` by one row per feed item, exactly
as a real run would. This behavior is **inherited from the original
code** — the agent didn't introduce the unconditional insert — but the
agent's own new README text asserts the opposite ("it does not touch the
database", `README.md:13-14`), which is new and incorrect. It does not
affect the actual dedupe fix (the `sent` table, which is what matters for
"stop re-sending"), but it is a factual error in documentation the agent
wrote, in a codebase whose stated goal for this task was leaving the next
person with an accurate understanding.

No other functional bugs found. The dedupe mechanism itself — the core of
the task — held up under every scenario I threw at it: repeat-run no-op,
snapshot-a → snapshot-b partial delivery, mid-run channel crash and
recovery, and migration onto a pre-existing database.

```json
{
  "identity_strategy": 10,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 7.5,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 3,
  "total": 38.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Correct per-feed identity and retry semantics; README wrongly claims dry-run leaves DB untouched."
}
```
