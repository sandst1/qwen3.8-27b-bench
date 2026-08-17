# Review: notify-digest dedupe fix (run-2)

## 1. Summary

The agent introduced a per-format `key` field in `feeds.py` (entry_id / permalink / link, chosen per-format for stability against each format's known instability), added a `sent(source, key, channel)` table in `store.py` that is written only after a channel's delivery succeeds, and rewrote `run_once` in `digest.py` to filter each channel's selection against its own `sent` set. It also fixed the pre-existing unconditional-insert bug in `record_items`, added a schema migration for existing databases, and documented the whole scheme in the README under a new "De-duplication" section. I verified the core claim against the fixtures (rotating `utm_campaign`, regenerated `guid`, edited-in-place blogroll post) and it works correctly — no duplicates, no missed items. I would merge this with one fix: an uncaught `DeliveryError` from one channel currently aborts the run before any later channel in the config is even attempted, every tick, until the broken channel is fixed.

## 2. Per-category scoring

### Identity strategy — 9.5 / 10

Per-format identity, chosen specifically to survive each format's stated failure mode, `feeds.py:47-93`:

```python
"key": str(r["entry_id"]),   # newsroom — entry_id is stable; the URL carries rotating utm_*
...
"key": r["permalink"],        # blogroll — no id of any kind except the permalink
...
"key": r["link"],             # generic — provider regenerates guid on edit; link is stable
```

I confirmed this against the fixtures. `fixtures/snapshot-b/newsroom.json` rotates `utm_campaign` from `w33`→`w34` on the same `entry_id`; `wire.json` regenerates `guid` (`wire-2026-08-14-0031` → `...-r2`) on the same `link`; `blogroll.json` edits title/excerpt in place on the same `permalink`. Running snapshot-a then snapshot-b against the same DB produced exactly one new item (a genuinely new newsroom entry) and zero duplicates for the three edited/rotated items — the fixture-level test the task explicitly suggested. The one thing keeping this from a 10: `r["entry_id"]` and `r["permalink"]` are direct indexing with no fallback if a field is ever absent (matches the original code's style, not a regression, but there's no "fallback chain" or defensive `.get()` as the top-band descriptor calls for).

### Ambiguity handling — 6 / 8

- **Per-channel vs global suppression**: named and resolved explicitly. Schema: `sent(... channel TEXT NOT NULL, PRIMARY KEY (source, key, channel))` (`store.py:41-47`), and stated in README: "Each channel only receives items it has not already been sent."
- **Edited item as new vs same**: named and resolved explicitly. `feeds.py` comments plus the README table ("the provider regenerates `guid`s whenever an item is edited") make clear edited items are treated as the *same* item, keyed by the stable field.
- **First-run backfill**: not named anywhere. On a fresh DB the `sent` table is empty, so the first run sends every item currently in the feeds as if new — a defensible choice, but it is made silently; nothing in the README or code says what a fresh deploy does on tick one.

Two of three forks are explicit, one is silent-but-reasonable — solidly mid-band, not top.

### Failure-mode reasoning — 6 / 8

Per-channel marking after successful send, with the ordering explicitly argued, `digest.py:70-75`:

```python
channels.send(chan_cfg, body)
# Mark after the send succeeds: if delivery fails, the items stay
# unsent and the next cron tick retries them, while channels that
# already succeeded do not re-send.
store.mark_sent(db, chan_cfg["name"], selected)
```

This is the correct ordering and the at-least-once tradeoff is stated. However I reproduced a real gap: `channels.send` can raise `channels.DeliveryError`, and `run_once` has no try/except around it. I built a 3-channel config where the second channel is an unreachable webhook:

```
$ python3 digest.py --config sim_config.toml --db sim.sqlite3
=== ops ===          # channel 1: sent and marked correctly
Traceback (most recent call last):
  ...
channels.DeliveryError: broken: <urlopen error [Errno 61] Connection refused>
EXIT: 1
```

`energy` (channel 3, listed after the broken one) was **never attempted** — not delayed, not attempted-and-failed, simply never reached because the process crashed first. Since `ops` gets re-marked as already-sent, and `broken` keeps failing every tick, `energy` will keep being skipped on every subsequent run too, indefinitely, until `broken` is fixed or reordered — this is a starvation bug on channels ordered after a persistently-broken one, not just a one-tick delay. It is inherited from the original (`channels.py`'s own docstring already says "we let the exception propagate"), so it isn't something the agent broke, but the task was specifically about repeat-sends and the agent's own file (`digest.py`) is exactly where this should have been caught with a per-channel try/except — it wasn't, and it isn't mentioned anywhere.

### Existing-code respect — 5.5 / 6

The unconditional-insert problem called out in the rubric is exactly the bug in the original `store.record_items` (it inserted every fetched item every 15 minutes with no existence check). The agent fixed it directly, `store.py:73-90`:

```python
def record_items(conn, source, items):
    """Archive items we haven't seen before (one row per (source, key))."""
    existing = {
        row["key"]
        for row in conn.execute("SELECT key FROM items WHERE source = ?", (source,))
    }
    new = [i for i in items if i["key"] not in existing]
    if not new:
        return
    conn.executemany(...)
```

`feeds.py` was extended, not rewritten — the original per-format branches and comments are preserved verbatim, just with one added key. `--dry-run` was preserved and verified (see Bugs section) to not write to `sent`. A migration was added for existing DBs (`store.py:62-70`) rather than assuming a fresh schema. Minor deduction: the `raw_id` column is kept in the schema and populated but is now largely redundant with `key` for two of three formats (`newsroom` and `generic` — `raw_id` and `key` are identical there); it's defensible as provenance, but nobody explains why both are kept.

### Code quality — 4 / 4

Clean, small diff. Sensible SQL (`INSERT OR IGNORE` on the `sent` table with a composite primary key rather than a manual existence check, `store.py:101-106`). No dead code. The migration function is guarded and commented on exactly why it must run before `executescript`:

```python
# Must run before executescript: the schema indexes `items.key`, which
# does not exist on DBs created before that column did.
```

I ran this against a hand-built legacy DB (pre-`key` schema) and it migrated and produced correct results with no exceptions.

### Documentation — 3 / 4

The README gained a full "De-duplication" section (not just a docstring) explaining the mechanism, the per-format key table, and the delivery-ordering rationale — this alone would clear the "docstring caps at 2" floor by a wide margin. What's missing: no instructions for how to reset the dedupe state (e.g., "delete the `sent` table / drop the DB to resend everything"), and no explicit statement of first-run behavior (a fresh DB sends everything as if new). Both are asked for directly in the rubric and neither appears in the README or code comments.

## 3. What it missed

- **First-run backfill** is not named as a decision anywhere (see Ambiguity handling).
- **Reset procedure** is undocumented — an operator wanting to force a resend has to reverse-engineer that dropping rows from `sent` (not `items`) is the right lever.
- **Channel failure isolation**: no try/except around `channels.send` in `run_once`, so one broken channel starves every channel listed after it in the config, indefinitely, every tick (see Bugs).
- The `raw_id` column's continued purpose alongside `key` is not explained now that `key` does the identity work `raw_id` used to be a stand-in for.
- No test file was added to lock in the dedupe behavior (the original had none either, so this isn't a regression, but the fixtures make an obvious basis for one and the task even suggests using them this way).

## 4. Bugs

**Uncaught channel failure blocks all subsequent channels, every tick, indefinitely.** Reproduction:

```toml
[[feeds]]
name = "newsroom"
format = "newsroom"
url = "file://fixtures/snapshot-a/newsroom.json"

[[channels]]
name = "ops"
type = "stdout"
keywords = []

[[channels]]
name = "broken"
type = "webhook"
url = "http://127.0.0.1:1/nope"
keywords = []

[[channels]]
name = "energy"
type = "stdout"
keywords = []
```

```
$ python3 digest.py --config sim_config.toml --db sim.sqlite3
=== ops ===
... (2 items printed, correctly marked sent)
Traceback (most recent call last):
  ...
channels.DeliveryError: broken: <urlopen error [Errno 61] Connection refused>
$ echo $?
1
```

`energy` never runs. Because `ops` is now marked sent and `broken` will keep failing, every future tick repeats exactly this: `ops` is silently skipped (already sent), `broken` crashes again, `energy` is never reached. This isn't a one-tick delay, it's a hang for every channel positioned after a persistently-broken one in the config, until someone notices and fixes or reorders it. Not introduced by this change (the original had the same unguarded `channels.send` call), but it directly bears on the failure-mode question the task implicitly raises, and the agent's own new code comment right above the crash site talks about retry semantics without addressing this.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 6,
  "existing_code_respect": 5.5,
  "code_quality": 4,
  "documentation": 3,
  "total": 34,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid per-feed identity dedupe; one uncaught-exception channel-starvation bug remains."
}
```
