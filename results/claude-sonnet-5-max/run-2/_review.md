# Review — notify-digest, claude-sonnet-5-max, run-2

## 1. Summary

The agent correctly diagnosed the bug as two separate problems — no stable item identity, and no record of what's already been delivered per channel — and fixed both: a `dedupe_key` derived from a tracking-param-stripped, source-qualified link in `feeds.py`, and a new `deliveries(channel, dedupe_key)` table in `store.py` that gates what actually gets sent. It also handled the in-place schema migration for the existing production `digest.sqlite3`, and documented every non-obvious decision (identity choice, per-channel vs global, edited-item handling, first-run backfill, crash semantics) in the README and in docstrings, rather than leaving them implicit. I verified all of this by running the code against both fixture snapshots, seeding a pre-migration legacy db, and simulating a mid-batch channel failure; every behavior matched what the README claims. I would merge this as-is.

## 2. Per-category scoring

### Identity strategy — 9.5/10

`feeds.py:50-66` (`dedupe_key`) explicitly reasons through all three feeds and picks the one thing that's stable across all of them — a normalized link:

```python
def dedupe_key(source, item):
    """The identity we key "already seen / already sent" state on.

    We deliberately do NOT use the provider's raw_id here:

    - blogroll gives us no id at all.
    - generic (wire) gives us a guid, but it regenerates it whenever the
      item is edited (see fetch() below), so keying on it would treat every
      correction as a brand new item and re-notify people for it.
    - newsroom's id is actually stable, but its link isn't (tracking params
      change run to run) -- so we can't use raw link either.

    A source-qualified, tracking-params-stripped link turns out to be
    stable across all three feed formats and is always present, so it's
    the one thing we can rely on everywhere.
    """
    return f"{source}:{normalize_link(item['link'])}"
```

I ran all three failure modes named in that docstring directly against `fixtures/snapshot-b`, which mutates all three feeds exactly this way (wire `guid` gets a `-r2` suffix, newsroom's `utm_campaign` rotates `w33→w34`, blogroll's link stays put but the title/excerpt change):

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ # switch config to snapshot-b, same db
$ python3 digest.py --config config.toml --db digest.sqlite3
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]   # the one genuinely new item
sent 2 items
```

Every edited-but-not-new item across all three formats was correctly treated as the same item and not resent; only the one genuinely new newsroom entry went out. `normalize_link` (`feeds.py:39-47`) sorts and strips a documented set of tracking params and drops fragments, which is a sound general approach.

Half a point off because the tracking-param stripping is a fixed allowlist (`_TRACKING_PARAMS`, `feeds.py:33-36`); a provider introducing a new cache-busting or session param outside that list would silently defeat dedup for that feed. This isn't addressed anywhere, though it's a narrow enough risk not to be a bigger deduction.

### Ambiguity handling — 8/8

All three forks named in the rubric are identified and resolved explicitly, not just correctly:

- **Per-channel vs global suppression**: `store.py:9-14` — "keyed on `(channel, dedupe_key)` rather than `dedupe_key` alone because the same item can legitimately go to more than one channel... each channel needs to see it once, independently of the others." I verified this: an item matching both `ops` and `everything`'s keyword filters is sent to both, each exactly once (see `_ === everything ===_` output in the identity test above, showing the same items as `ops` plus more).
- **Edited item as new vs same**: covered by the `dedupe_key` docstring above and explicitly restated in README.md:41-48 ("What is stable... is the article link, once tracking query params... are stripped").
- **First-run backfill**: README.md:79-85 states plainly that the first run after deploying against an existing db with no `deliveries` history "will treat everything currently archived as unsent and send it once per matching channel — a one-time, bounded catch-up digest," and gives an escape hatch (clear `items` or point `--db` at a fresh file) for anyone who doesn't want that. I reproduced this by seeding a synthetic legacy db and confirmed the catch-up send happens once and only once.

### Failure-mode reasoning — 8/8

`digest.py:60-65`:

```python
channels.send(chan_cfg, body)
# Only mark delivered once send() has actually succeeded (it raises
# on failure -- see channels.py) so a failed send is retried next
# tick instead of being silently swallowed.
store.mark_sent(db, chan_cfg["name"], selected)
sent += len(selected)
```

Marking is per-channel and strictly after the corresponding `send()`, and each `mark_sent`/`record_items` call commits immediately (`store.py:107,134`), so the effect is durable per-channel as the loop progresses. I simulated a real partial failure — `energy`'s `send()` raising after `ops` already succeeded:

```
=== ops ===
Ops digest — 4 item(s)
...
run crashed with: energy webhook down
--- deliveries table ---
{'channel': 'ops', 'dedupe_key': 'newsroom:...port-fees-union', ...}
{'channel': 'ops', 'dedupe_key': 'blogroll:...port-fee-arithmetic', ...}
{'channel': 'ops', 'dedupe_key': 'wire:...i/0031', ...}
{'channel': 'ops', 'dedupe_key': 'newsroom:...port-fees', ...}
```

`ops`'s deliveries are durably recorded, `energy` and the not-yet-reached `everything` are not — they'll retry on the next cron tick, correctly, with no items lost or double-sent. The README (README.md:65-68) states the at-least-once tradeoff plainly: "If a channel's `send()` fails... it's never marked as delivered, so that channel gets another shot at those items on the next cron tick." This is exactly the top-band behavior the rubric asks for, argued in prose, not just implemented.

### Existing-code respect — 6/6

`render.py`, `channels.py`, and `config.example.toml` are untouched (confirmed via diff). `digest.py`'s control flow is minimally extended (`candidates`/`selected` split, one `mark_sent` call) rather than restructured. The `items` archive table is kept and its "we insert unconditionally" problem is dealt with directly rather than ignored: `record_items` switches to `INSERT OR IGNORE` keyed on a new unique index over `dedupe_key` (`store.py:34, 90-93`), and a `_migrate_legacy_items` function (`store.py:55-86`) handles upgrading an existing production db in place — backfilling `dedupe_key` from `source+link` and collapsing pre-existing duplicate rows before the unique index is created, so deploying against the live `digest.sqlite3` mentioned in the prompt doesn't crash. I seeded a synthetic legacy `items` table with 3 duplicate rows for one item and confirmed the migration collapsed them to 1 row and computed the correct `dedupe_key` without error. `--dry-run` still works and, correctly, never calls `mark_sent` (verified: two consecutive `--dry-run` invocations produce identical output).

### Code quality — 4/4

No dead code; `raw_id` is kept in the schema and item dict but is explicitly justified as "kept only for reference" (`feeds.py:12`) rather than vestigial. SQL is straightforward parameterized `executemany`/placeholder-list queries (`store.py:90-93, 117-121, 130-133`). The schema migration is isolated in one clearly-named, well-commented function and runs conditionally (no-op on both a fresh db and an already-migrated one — verified both cases). `python3 -m py_compile` on all touched files succeeds.

### Documentation — 4/4

README.md gains a full "Avoiding repeat sends" section (README.md:31-69) explaining the two mechanisms, why `raw_id` doesn't work, why tracking suffix stripping is needed, why per-channel not global, and a "Deploying this to the existing cron job" subsection (README.md:70-85) that walks through exactly what happens to the box's existing `digest.sqlite3` on upgrade, including the one-time catch-up send and how to avoid it. This goes beyond docstring-only documentation and is the kind of explanation the rubric's top band asks for.

## 3. What it missed

- **Fixed tracking-param allowlist** (`feeds.py:33-36`): a provider adding a new query param that isn't `utm_*`/`gclid`/`fbclid` (e.g. a rotating session or cache-bust token) would silently break dedup for that feed again. Not discussed anywhere as a residual risk.
- **No consideration of what happens if a whole feed is unusually slow/hangs `--dry-run` forever, or the sqlite file is on a networked mount** — not asked for by the prompt, but also not flagged as an assumption.
- **`deliveries` table has no retention/pruning.** It grows forever, one row per (channel, item) ever sent. For a 15-minute cron job over years this is a real but slow-burning storage concern that isn't mentioned.
- **No test suite added.** The agent verified behavior manually (per its logs) but left nothing runnable a future maintainer could execute to confirm the dedupe logic still works after a change to `feeds.py`. Given the codebase has no existing tests either, this is defensible, but it's a silent decision, not an argued one.

## 4. Bugs

None found. I attempted to break it three ways and each behaved as documented:

1. **Snapshot-a → snapshot-b transition** (guid regen, utm rotation, title edit) — only the genuinely new item was sent; reproduction above under Identity strategy.
2. **Legacy db migration** — seeded a pre-`dedupe_key` `items` table with 3 duplicate rows for the same link; `_migrate_legacy_items` collapsed them to 1 and the first run correctly did a one-time backfill catch-up send, with the second run sending 0. Reproduction under Existing-code respect.
3. **Mid-batch channel failure** — `energy.send()` raising `DeliveryError` after `ops` succeeded left `ops`'s deliveries durably committed and `energy`/`everything` untouched for retry; reproduction under Failure-mode reasoning.

No exceptions, no double-sends, no lost items in any of these.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 4,
  "total": 39.5,
  "would_merge": "yes",
  "one_line": "Correct per-feed identity, per-channel delivery tracking, migration, and reasoning all explicit."
}
```
