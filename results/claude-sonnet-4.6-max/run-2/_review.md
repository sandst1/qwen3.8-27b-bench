# Review — notify-digest dedup fix

## 1. Summary

The agent implemented a `store.seen_before()` gate keyed on `(source, link, raw_id)`, called it from `digest.py` before the channel-send loop, wrote a genuinely thoughtful docstring/README section explaining why each of the three feed formats needs a different stable field, and added an 11-test suite that exercises all three fixtures scenarios (UTM rotation, guid regeneration, no-id blogroll). The identity logic itself is correct and I verified it against the fixtures. However, the placement of `store.record_items()` — before any channel is attempted, and unconditional on `--dry-run` — means a `--dry-run` invocation permanently poisons the dedup state for the real run that follows, and a mid-run delivery failure on channel N silently and permanently drops the batch for every channel from N onward. I would not merge this as-is: the core idea is right, but it ships with two latent data-loss bugs that a cron job will trigger in practice and that the agent never tested for or mentioned.

## 2. Per-category scoring

### Identity strategy — 9/10

The agent recognized that no single field is stable across all three formats and chose an OR-combined key, scoped per `source`, in `store.py:56-68`:

```python
def seen_before(conn, item):
    row = conn.execute(
        "SELECT 1 FROM items"
        " WHERE source = ?"
        "   AND (link = ? OR (raw_id IS NOT NULL AND ? IS NOT NULL AND raw_id = ?))"
        " LIMIT 1",
        (item["source"], item["link"], item.get("raw_id"), item.get("raw_id")),
    ).fetchone()
    return row is not None
```

The failure modes are stated explicitly in the module docstring (store.py:7-27) and mirrored in the README table:

```
| `newsroom` | `raw_id` (entry_id) | URL has rotating UTM campaign tags |
| `generic` (wire) | `link` | Provider regenerates the guid on every edit |
| `blogroll` | `link` (permalink) | No id field exists |
```

I ran it against both fixture snapshots to check this isn't just asserted:

```
$ python3 digest.py --config config-b.toml --db digest.sqlite3   # after having ingested snapshot-a
sent 2 items
* Union responds to port fee inquiry  [newsroom]   (entry_id 84130 — genuinely new)
```

Confirmed via the raw fixtures that entry 84121 (UTM `w33`→`w34`, same `entry_id`), the wire item with guid `-r2` suffix (same `link`), and the blogroll post with updated excerpt (same `permalink`) were all correctly suppressed, while the one truly new newsroom entry was sent. `test_null_raw_id_does_not_cross_match` in `test_dedup.py` also covers the blogroll NULL-`raw_id` case. This is a real fallback-style identity, tested against all three fixtures, with its tradeoffs written down — solidly in the top band. Half a point off because it's expressed as one OR-query rather than a per-format dispatch function, which means a coincidental cross-item `raw_id`/`link` collision inside the same source (not exercised by the fixtures) would silently over-suppress; a fully "per-feed" implementation would branch on format explicitly rather than relying on the OR to happen to be safe.

### Ambiguity handling — 3/8

Of the three forks the rubric names:

- **Edited item as new vs. same** — resolved and stated explicitly (README "Deduplication" section, store.py docstring). This is the one fork the agent clearly named.
- **First-run backfill** — not discussed anywhere. What a fresh, empty `digest.sqlite3` does on the very first cron tick (send the entire current feed contents as "new") is never mentioned in the README or code comments. It happens to be reasonable behavior, but it's silent.
- **Per-channel vs. global suppression** — decided silently, and decided the way the rubric explicitly calls out as the common failure: dedup is computed once per item during the *fetch* loop (`digest.py:40-45`), before any channel filtering or sending happens. There is no per-channel "was this delivered to channel X" state at all — an item is "seen" globally the instant it's fetched, regardless of which (if any) channels actually received it. I demonstrated below that this is not just a naming problem but an actual bug (see Bugs). Per the rubric's own example ("typically global suppression"), this places the run in the 2-3 band.

### Failure-mode reasoning — 2/8

`channels.py`'s existing docstring says: "Delivery is best effort: if a channel is down we let the exception propagate and cron will pick us up again on the next tick." The agent didn't touch `channels.py`, but the new dedup gate breaks the "cron will pick us up again" promise, because marking-as-seen happens in the fetch loop (`digest.py:43-45`), fully before the channel-send loop even starts:

```python
new_items = [i for i in items if not store.seen_before(db, i)]
store.record_items(db, feed_cfg["name"], new_items)   # committed here
all_items.extend(new_items)
...
for chan_cfg in cfg["channels"]:
    ...
    channels.send(chan_cfg, body)   # may raise; already-committed items are lost for any channel not yet reached
```

I reproduced the "channel two fails after channel one succeeded" scenario directly:

```
$ python3 -c "... monkeypatch channels.send to fail on channel 'energy' ..."
=== ops ===          # delivered fine
run_once raised: simulated webhook outage
channels attempted before crash: ['ops', 'energy']
archive count after crash: 6
$ python3 digest.py --config config.example.toml --db digest2.sqlite3   # retry, as cron would
sent 0 items
```

`ops` got its digest. `energy` crashed and never got its items. `everything` (the third channel) never even ran. On retry, all 6 items are already in the archive, so `seen_before()` silently drops them for `energy` and `everything` forever — this is a whole-batch, per-run marking failure exactly as described in the rubric's 2-3 band, and it is *worse* than "per-run" because the mark happens even earlier than the send loop, before any channel is attempted at all. There is zero discussion of this tradeoff anywhere in the diff.

### Existing-code respect — 2/6

The change is minimally invasive to `feeds.py`, `channels.py`, and `render.py` (untouched), and reuses the existing `items` archive rather than adding a parallel table — good instincts, per the rubric's explicit guidance that reusing `items` is fine "if the unconditional insert is dealt with." But the unconditional insert is only half-dealt-with: it's now conditional on *dedup* but still unconditional on `--dry-run`, and the rubric explicitly calls out "breaking `--dry-run`" as a existing-code-respect violation. I confirmed this:

```
$ python3 digest.py --config config.example.toml --db digest3.sqlite3 --dry-run > /dev/null
$ python3 -c "import store; print(store.count_items(store.connect('digest3.sqlite3')))"
6
$ python3 digest.py --config config.example.toml --db digest3.sqlite3   # the "real" run, same db
sent 0 items
```

A `--dry-run` preview — explicitly documented as "prints what would be sent instead of sending it" — now has a permanent side effect: it commits the fetched items to the dedup store, so the very next real run (against the same db) sends nothing. This is a direct regression of a documented, previously side-effect-free flag, caused by `store.record_items` sitting above the `if dry_run` branch in the same loop that now also does the dedup filtering. That's a meaningful bulldoze of an existing guarantee, even though the code around it looks tidy.

### Code quality — 3/4

The code is readable, has no dead code or vestigial columns, and no schema migration was needed since no new columns were added (`raw_id` already existed). The SQL is a plain parameterized query. Docked half a point for the query binding `item.get("raw_id")` twice as separate parameters where a named parameter would be clearer, and half a point because the `dry_run`/dedup interaction (above) reflects a missed edge case in an otherwise clean implementation rather than a pure style issue.

### Documentation — 2.5/4

There's a dedicated, non-trivial README "Deduplication" section (not just a docstring) with a per-format table and a plain-English explanation of the OR check, plus the `store.py` module docstring goes into more depth. That clears the "docstring alone caps this at 2" floor. But it doesn't say how to reset the dedup state (delete/rotate the db file — never mentioned), doesn't describe first-run behavior, and says nothing about the `--dry-run` interaction or the partial-delivery-failure behavior documented above — exactly the parts a "next person to touch it" would need to know before deploying this.

## 3. What it missed

- **Per-channel delivery state.** There is no notion of "sent to channel X" at all; dedup is purely "fetched at least once," which is why a failed/never-reached channel loses items forever (see Bugs).
- **`--dry-run` is no longer side-effect-free.** Never tested, never mentioned.
- **Reset/backfill instructions.** No documentation of how to re-send an item (delete a row, wipe the db) or what the very first cron run against an empty db does.
- **At-least-once vs. at-most-once.** The system now silently implements "at-most-once, and sometimes zero-times-for-some-channels," without ever stating that this is the chosen guarantee or weighing it against the alternative (mark only after all channels succeed, which would restore at-least-once but risk duplicate sends on partial retry).

## 4. Bugs

**Bug 1 — `--dry-run` poisons the dedup state for the next real run.**
Repro:
```sh
python3 digest.py --config config.example.toml --db digest.sqlite3 --dry-run   # just a preview...
python3 digest.py --config config.example.toml --db digest.sqlite3            # ...but now sends 0 items
```
Root cause: `store.record_items()` (digest.py:44) is called before the `if dry_run:` check further down in the same function, so a preview run commits real archive rows.

**Bug 2 — a mid-run channel failure permanently drops the batch for every channel not yet reached.**
Repro (channel `energy` simulated to fail after `ops` succeeds):
```
channels attempted before crash: ['ops', 'energy']
archive count after crash: 6
# retry:
sent 0 items
```
Root cause: dedup marking happens in the fetch loop (digest.py:40-45), fully before the channel-send loop, so items are "seen" the moment they're fetched — independent of whether any channel actually received them.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 3,
  "failure_mode_reasoning": 2,
  "existing_code_respect": 2,
  "code_quality": 3,
  "documentation": 2.5,
  "total": 21.5,
  "would_merge": "no",
  "one_line": "Correct identity logic, but dry-run and partial-failure paths silently lose items."
}
```
