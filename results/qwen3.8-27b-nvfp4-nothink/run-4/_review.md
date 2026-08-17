# Review — notify-digest dedupe fix

## 1. Summary

The agent added a `sent` table keyed by `(source, item_key)` where `item_key = raw_id or link`, checked before delivery and written after each channel's `send()` succeeds, plus a `--all` bypass flag and a new README "Deduplication" section. The idea is sound in outline, but it breaks in two concrete, reproducible ways: the schema migration crashes on any database that already has the duplicate rows this exact bug produces, and the identity key silently resends edited items on the one feed format (`wire`/`generic`) whose docstring explicitly warns about that. I would not merge this as-is — it needs the migration fixed before it's safe to run against the real `digest.sqlite3`, and the identity choice for the `generic` feed reversed to match its own stated rationale.

## 2. Per-category scoring

### Identity strategy — 5/10

`store.py`:
```python
def item_key(item):
    """Per-feed identity of an item. See the module docstring for why."""
    return item.get("raw_id") or item["link"]
```
The module docstring claims the opposite of what this function does:
```
Keying on the link (rather than raw_id) for feeds that have ids is
deliberate: if a provider ever changes the id scheme, links are the last
thing to break, and we would rather re-send an edited item than
silently drop it.
```
But `item_key` returns `raw_id` first whenever it's present — for `wire` (`generic` format), `raw_id` is the `guid`, and `feeds.py`'s own docstring says: *"has a guid, but the provider regenerates it whenever an item is edited."* So the one feed flagged in the codebase as having an unstable id is keyed on that unstable id anyway.

Reproduced with the fixtures:
```
$ python3 digest.py --config config-a.toml --db digest.sqlite3   # snapshot-a
sent 12 items          # includes "Port fee inquiry opened [wire]" guid=...-0031

$ python3 digest.py --config config-b.toml --db digest.sqlite3   # snapshot-b, same item edited
=== ops ===
Ops digest — 2 item(s)
* Union responds to port fee inquiry  [newsroom]
* Port fee inquiry opened  [wire]        <-- same link, same content plus one sentence, guid now "-r2"
sent 4 items
```
The wire item, edited only to add "Adds operator comment," is delivered again — exactly the "same item over and over" the task complains about. `newsroom` happens to work only because its `entry_id` is genuinely stable in the fixtures (unrelated to the stated design, which says to prefer link). `blogroll` works because it's the only case with no id, so the fallback is forced. This is "single strategy works for most items, breaks on the feed it doesn't" (6-8 band on paper), but I'm marking it below that band because the failure is the specific scenario the codebase's own comments called out, the agent's own docstring describes the opposite of what the code does, and there's no sign this was ever run against the fixtures to check.

### Ambiguity handling — 3/8

Three forks, one at a time:

- **Per-channel vs. global suppression** — named and resolved explicitly:
  ```python
  # Key by feed name, not channel name: an item is "sent" once
  # it has been delivered, period. The channel is irrelevant to
  # dedup (and one item can match several channels at once).
  ```
  Named, but — per the benchmark's own scoring notes — a global-suppression choice like this is flagged as the classic wrong answer, and it is (see Failure-mode reasoning below: it causes silent, permanent loss of delivery to a second channel after a crash).
- **Edited item as new vs. same** — addressed, but self-contradictory: the module docstring says link should win for stability, the README dedup section says the same ("Keying on the link rather than the raw id is deliberate"), and the code does the opposite (`raw_id or link`). A decision that isn't reliably reflected in the code isn't really "resolved."
- **First-run backfill** — not named anywhere. Running against an empty DB just sends everything (verified: first run against snapshot-a sent all 12 items across channels with no comment on this being deliberate/acceptable behavior for a fresh deploy).

One fork resolved-and-flagged-as-wrong-by-the-rubric, one resolved-but-contradicted-by-its-own-code, one not addressed at all.

### Failure-mode reasoning — 3/8

`digest.py`:
```python
channels.send(chan_cfg, body)
if dedupe:
    for i in selected:
        store.mark_sent(db, i["source"], {store.item_key(i)})
sent += len(selected)
```
Marking happens after `send()`, which is the right instinct for at-least-once per channel — except marking is keyed by `(source, key)` only, with no channel dimension at all. So if a channel earlier in the loop succeeds and a later one crashes, the crashed channel's item is already marked "sent" for that feed and will never be retried, even though it was never delivered to that channel.

Reproduced: with `ops` before `energy` before `everything` in `cfg["channels"]`, made `energy`'s send raise mid-run:
```
=== ops ===                                          # succeeds, marks 3 items sent
Ops digest — 3 item(s)
...
crashed: simulated crash on energy channel
```
Retried the run with the channel fixed:
```
=== energy ===
Energy digest — 3 item(s)
...
=== everything ===
Firehose — 3 item(s)          # <-- should be 6; the 3 "ops" items are gone forever
```
The `everything` (firehose) channel, which matches every item, never received the three port-fee items and never will — they were marked "delivered" by `ops`'s earlier success alone. This is exactly the "partial-failure window that loses ... items" the rubric describes, and it isn't discussed anywhere in the README or comments.

### Existing-code respect — 4.5/6

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are untouched. `--dry-run` still works and still doesn't write to `sent`:
```
$ python3 digest.py --config config-a.toml --db digest-dry.sqlite3 --dry-run
--- would send to ops ---
...
items: 6
sent: 0
```
The pre-existing "unconditional insert into `items`" issue that the rubric calls out is addressed:
```python
"INSERT OR IGNORE INTO items (source, raw_id, title, link, summary, published)"
```
backed by a new unique index. Reasonable, minimal footprint, no scope creep beyond the `--all` flag (which is a sensible, on-topic addition). Docked half a point for the migration failure below, which is squarely "does it work with the existing (deployed) database."

### Code quality — 1/4

The new schema:
```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_key
    ON items(source, COALESCE(raw_id, link));
```
This is added unconditionally on every `connect()`. The task's premise is that this cron job has been running every 15 minutes and re-inserting the same items — i.e. the `items` archive of any real deployment almost certainly already has duplicate `(source, raw_id)` rows. Simulated that:
```
$ python3 -c "
import store
db = store.connect('/tmp/prod-like.sqlite3')   # items table with 5 dup rows, pre-existing
"
Traceback (most recent call last):
  ...
sqlite3.IntegrityError: UNIQUE constraint failed: index 'idx_items_source_key'
```
Deploying this change against the actual production `digest.sqlite3` this task is about will crash on the very first invocation after upgrade, taking the cron job down entirely — the opposite of "fix the duplicates." There is no migration step, no `INSERT OR IGNORE`-based cleanup of the existing table, no `try/except` around index creation. This is the single most damaging bug in the change and is exactly what "schema migration handled for an existing database" in the rubric is asking about.

### Documentation — 2/4

There's a real README section (not just a docstring), which is more than the 2-point cap requires structurally:
```
## Deduplication
...
- `key` is the feed's raw id when the feed provides one, else the item
  link. ... Keying on the link rather than the raw id is deliberate...
```
But it directly misdescribes the code (see Identity strategy above: the code checks raw_id first, the prose says link should win), doesn't mention that `--all` doesn't reset anything, and doesn't say what a first run does. A reader trusting this section will misunderstand the actual dedupe behavior. Capped at 2 for the self-contradiction, even though it's more than a docstring.

## 3. What it missed

- **First-run behavior** is never named as a decision (send-everything on an empty DB is the de facto choice, silently).
- **Reset instructions**: no `DELETE FROM sent` or "drop the sent table to force a resend" guidance; `--all` is documented as not resetting anything, but nothing tells you what does.
- **Migration path for an existing, already-duplicated `items` table** was not considered at all, despite being squarely implied by the prompt ("keep getting the same items over and over" = the archive already has dupes).
- The mismatch between the store.py/README's stated rationale ("key by link, not raw_id, to avoid resending edits") and what `item_key` actually does suggests the fixtures were not run end-to-end after the change, or the discrepancy would have surfaced immediately.

## 4. Bugs

1. **Crash on any pre-existing database with duplicate archive rows** (see Code quality above) — `store.connect()` raises `sqlite3.IntegrityError` when creating `idx_items_source_key` against data that the described bug would have already produced. Reproduction included above.
2. **Wire/generic feed items are re-sent on edit** — the exact "same item over and over" symptom the task is about, for the one feed format explicitly flagged as unstable. Reproduction: run against `fixtures/snapshot-a` then `fixtures/snapshot-b`; the wire item `wire-2026-08-14-0031` → `wire-2026-08-14-0031-r2` is delivered twice.
3. **Permanent, silent loss of delivery to a channel processed after a crash** — reproduced above: `ops` succeeds, `energy` crashes, retry never re-delivers the ops-matched items to the `everything` channel, because `mark_sent` is channel-agnostic.

```json
{
  "identity_strategy": 5,
  "ambiguity_handling": 3,
  "failure_mode_reasoning": 3,
  "existing_code_respect": 4.5,
  "code_quality": 1,
  "documentation": 2,
  "total": 18.5,
  "would_merge": "no",
  "one_line": "Dedupe crashes on real DBs with existing dupes; edited wire items still resend."
}
```
