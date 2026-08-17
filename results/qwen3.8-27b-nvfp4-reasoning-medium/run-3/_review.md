# Review

## 1. Summary

The agent added a per-source `key` field computed by feed format (entry_id, permalink, or link — deliberately avoiding guid/URL fields the providers rotate) and a new `sent(channel, source, key)` table that gates delivery per channel, marked only after a successful send. It named and resolved all three ambiguity forks explicitly (per-channel suppression, edited-item-as-same via key choice, first-run backfill) in both code comments and a new README section, and verified against the fixtures the diff behaves correctly under `utm_campaign` rotation, guid-suffix changes, and edited excerpts. I would merge this with one fix: an unhandled channel-send exception currently aborts the whole run and skips any channels ordered after the failing one, rather than being isolated per channel.

## 2. Per-category scoring

### Identity strategy — 8.5/10

The agent picked a per-format identity ("key") explained inline in `feeds.py:9-21`:

```python
`key` is the item's stable identity within its source. The delivery
dedup in digest.py is keyed on it, so it must not change when a provider
re-presents the same item. It is deliberately chosen per format, because
the fields the providers offer are not all stable:

- newsroom: `entry_id`. The URLs carry rotating tracking parameters, so
  the URL would not do.
- blogroll: the permalink. There is no identifier of any other kind.
- generic: the link. The guid is regenerated whenever the provider edits
  an item, so an edited item would look brand new if we keyed on guid.
```

I verified this against the actual fixture diffs. `fixtures/snapshot-b` rotates the newsroom `utm_campaign` (`w33`→`w34`), regenerates the wire `guid` (`wire-2026-08-14-0031` → `...-r2`), and edits the blogroll title/excerpt in place — while `entry_id`, `link`, and `permalink` respectively stay constant. Running snapshot-a then snapshot-b against the same DB produces exactly one new item (`Union responds to port fee inquiry`) and correctly suppresses the three edited/rotated re-presentations:

```
$ python3 digest.py --config config-b.toml --db digest.sqlite3
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
...
sent 2 items
```

That's the intended failure mode called out and it holds. Docked 1.5 points, not for breakage but for shallowness: the identity is a single field per format with no fallback chain (e.g. if `entry_id` were ever missing, or if the `generic` feed's link itself changed for an edit, the key silently breaks — this isn't discussed). The chosen fields do happen to survive everything the fixtures throw at them, but "per-feed" here is really "one field, per feed" rather than a chain with graceful degradation.

### Ambiguity handling — 8/8

All three named forks are explicit in code and README, not just implicitly correct:

- **Per-channel vs. global suppression** — `digest.py:52-59`, `store.py` schema (`PRIMARY KEY (channel, source, key)`), and README ("An item that matches several channels is sent to each of them, once each.") State is genuinely per-channel; I confirmed this holds under partial failure (see Bugs).
- **Edited item as new vs. same** — directly discussed per-format in `feeds.py`'s module docstring and the README table, and is the entire reason for the key strategy rather than reusing guid/URL.
- **First-run backfill** — README states explicitly: *"First run after deploying this sends the current feed contents as a baseline (nothing is in `sent` yet); from then on only new items are sent."* This is a real, stated decision (not silent), and it's the sane default — nobody wants a scheduler to silently swallow everything already in a feed on first deploy.

This is the strongest category: no guessing required from the next reader.

### Failure-mode reasoning — 6/8

Per-channel marking is real and ordered correctly: `store.mark_sent` is called immediately after `channels.send` succeeds, inside the per-channel loop (`digest.py:66-72`), so a channel that fails doesn't get its items marked and will retry them next tick — this is stated explicitly: *"Mark only after the send succeeds: if it fails, the items stay unmarked and the next cron tick retries them (at-least-once)."*

However, I reproduced a real gap: `channels.send` raises `DeliveryError` on failure (`channels.py:35`), and `run_once` has no try/except around it. If channel B (of three) fails, the exception propagates uncaught out of `run_once` and `main`, and **channel C is never attempted this tick at all** — not because it failed, but because the loop was aborted:

```
$ python3 fail_test.py   # ops, then a broken webhook, then everything
=== ops ===
Ops digest — 3 item(s)
...
CRASHED: DeliveryError broken: <urlopen error [Errno 61] Connection refused>
ops sent-keys count: 3
everything sent-keys count: 0
```

No item is lost (everything is retried next tick along with `ops`'s state correctly preserved), so the at-least-once guarantee itself isn't violated. But this is a genuine failure-isolation gap the docstring doesn't disclose: one flaky webhook channel silently delays every channel that comes after it in `cfg["channels"]`, indefinitely, every 15 minutes, with an unhandled exception hitting cron's error output each time. A `try/except DeliveryError` around the per-channel send, logging and continuing to the next channel, would have closed this. Scored mid-band rather than top: marking and ordering are correct and the guarantee is argued, but partial-failure isolation across channels within one run is incomplete and unmentioned.

### Existing-code respect — 6/6

- `feeds.py` keeps its existing shape and per-format branching; only a `"key"` field is added to each dict (`feeds.py:64,80,96`), no rewrite.
- `items` table and `record_items`/`count_items` are untouched — the append-only over-insertion behavior is explicitly retained and reasoned about in the updated docstring rather than silently left as dead risk: *"an append-only log of everything we have seen (one row per item per run)... Nothing reads it automatically."* This directly satisfies the rubric's "if the unconditional insert is dealt with" bar — it's dealt with by acknowledgment, since it isn't used for dedup and isn't the source of the bug being fixed.
- `--dry-run` was verified still non-mutating: two consecutive dry runs against a fresh DB produce byte-identical output, and a subsequent real run still delivers all 12 items — proving dry-run genuinely never writes to `sent`.
- Schema migration for an existing DB is trivial and safe: `CREATE TABLE IF NOT EXISTS sent (...)` added alongside the existing `items` schema string, so an already-deployed sqlite file gains the new table without any destructive change or manual migration step.

No scope creep — no plugin system, no scheduler, no unrelated rewrite.

### Code quality — 3.5/4

Clean, idiomatic additions: `sent_keys` returns a set for O(1) membership tests, `mark_sent` uses `executemany` with `INSERT OR IGNORE` for idempotency and documents why (`store.py:78-86`), the `sent` table has a sensible composite primary key instead of a surrogate id + unique index. No dead code, no vestigial columns. Minor deduction: the `matches(i, chan_cfg) and (i["source"], i["key"]) not in seen` line in `digest.py:56-58` recomputes the tuple inline rather than being a small helper, and `sent_keys` is called fresh (one query) per channel per run — fine at this scale but not called out as a potential cost at large history sizes.

### Documentation — 4/4

Goes beyond a docstring: the README gets a new "## Deduplication" section covering the key table per format with rationale, and a bulleted list of the exact consequences ("First run after deploying," "Failed deliveries are not marked sent," "After downtime," "`--dry-run` previews exactly what a real run would send"). This is precisely the reset/first-run/behavior explanation the rubric asks for, in the right place (README, not buried in a function docstring), and it's consistent with what the code actually does (verified above).

## 3. What it missed

- **Cross-channel failure isolation.** As reproduced above, one channel raising mid-loop kills the whole run and skips channels ordered after it, rather than being caught and logged per channel. Not disclosed anywhere in the new docs. Easy one-line fix (`try/except DeliveryError` in the channel loop), and it's the one place the agent's own "at-least-once, per-channel" story doesn't fully hold up under test.
- **No fallback chain within a format.** If `entry_id` is ever absent for a newsroom row, or a `generic` item's link itself changes on edit, dedup silently breaks — there's no secondary key or documented behavior for that case. The rubric's top identity band wants exactly this discussed; the agent covers cross-format choice but not intra-format degradation.
- **No reset/backfill knob.** The README documents *what* happens on first run and after downtime but doesn't give an operator any way to force a re-send (e.g. `DELETE FROM sent WHERE channel=...` or a `--reset` flag) if someone needs to intentionally replay a digest. Not required by the rubric explicitly, but it's the natural next question a reader would have after reading "resets to a baseline on first run" and finding no lever to pull.
- **No test suite added.** The agent's correctness claims (verified independently here against the fixtures) aren't backed by a script or test the next person could re-run to confirm dedup still works after a future change to `feeds.py`.

## 4. Bugs

**One failing channel silently blocks all later channels for that tick.** Reproduction:

```python
# channels list: [ops (stdout), broken (webhook, unreachable), everything (stdout)]
digest.run_once(cfg, db, dry_run=False)
# -> ops delivers and is marked sent (3 items)
# -> broken raises DeliveryError, uncaught
# -> everything is never attempted this tick; 0 keys recorded for it
```
```
CRASHED: DeliveryError broken: <urlopen error [Errno 61] Connection refused>
ops sent-keys count: 3
everything sent-keys count: 0
```

No items are lost or duplicated (retried correctly next tick), so this isn't a correctness bug in the strict dedup sense, but it is a real operational bug: a single misconfigured or temporarily-down webhook channel will cause every cron tick to crash with an unhandled exception and starve every channel configured after it in `config.toml`, indefinitely, until that channel is fixed or reordered.

```json
{
  "identity_strategy": 8.5,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 6,
  "existing_code_respect": 6,
  "code_quality": 3.5,
  "documentation": 4,
  "total": 36,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid per-format key dedup, well documented, but one channel's failure blocks all later channels."
}
```
