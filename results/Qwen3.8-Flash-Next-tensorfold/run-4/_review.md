## 1. Summary

The agent added a `delivered` ledger table to `store.py` and keyed it on `(channel, source, canonical-link)`, deliberately ignoring the per-feed provider ids (`raw_id`/`guid`) because it noticed two of the three fixture feeds rotate or regenerate them; `digest.py` now skips already-delivered items per channel and only marks an item delivered after `channels.send()` returns without raising. It documented the identity choice, the per-channel-vs-global decision, and the first-run backfill behaviour in both `store.py`'s docstring and the README. I verified the dedupe against `fixtures/snapshot-a` → `snapshot-b` and a simulated mid-run channel failure; both worked exactly as described. I would merge this after fixing one factual error in the README (`--dry-run` does *not* behave as documented — see Bugs).

## 2. Per-category scoring

### Identity strategy — 9.5 / 10

The key is `(source, canonical link)`, with a documented fallback chain, defined in `store.py:76-81`:

```python
def item_key(item):
    """The (source, canonical link) pair identifying an item. See module docstring."""
    link = item["link"].split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if not link:
        link = item.get("raw_id") or item["title"]
    return (item["source"], link)
```

This is explicitly *not* a raw-id/guid dedupe, and the module docstring states why (`store.py:12-19`):

> "Identity: an item is identified by (source, canonical link), NOT by its provider id and NOT by its title/title+link, because the ids we get are not stable across repeats — the wire provider regenerates its guid whenever an item is edited, and newsroom appends a rotating utm_campaign to its urls — while title and link are stable. ... If the link is ever empty we fall back to raw_id, then title."

I confirmed this against all three feeds by running snapshot-a then snapshot-b through a scratch copy:

- `newsroom` rotates `utm_campaign=w33` → `w34` on the same story's URL — query-stripping handles it; the item is correctly suppressed on the second run and only the genuinely new "Union responds to port fee inquiry" item is sent.
- `wire` (the "generic" format) regenerates its `guid` (`wire-2026-08-14-0031` → `...-r2`) when the item is edited — correctly suppressed, because `raw_id`/guid isn't part of the key at all.
- `blogroll` has no id of any kind (`raw_id: None` always) and its title changes to "(updated)" between snapshots — correctly suppressed, because the stable `permalink` is what's keyed on.

That is the specific trio the rubric calls out (rotating `utm_campaign`, missing `raw_id`, regenerated `guid`), and all three are handled and the choice is stated with its failure mode (empty-link fallback). The half-point deduction: the docstring doesn't flag the implicit assumption that query strings never carry real identity (a feed that legitimately used `?id=` to distinguish two different stories would silently collide) — a minor but real edge case left unacknowledged.

### Ambiguity handling — 8 / 8

All three canonical forks are named and resolved, not just implemented:

- **Per-channel vs. global suppression** — `README.md:28`: "Matching is per channel: an item matching two channels is sent once to each." Implemented as `store.was_delivered(db, name, i)` keyed by channel in `digest.py:57`, not globally.
- **Edited item as new vs. same** — `store.py:16-18`: "why title is not part of the key: an edited or retitled story at the same link is the same story." Verified: blogroll's title changing to "(updated)" did not cause a resend.
- **First-run backfill** — `README.md:28-30`: "a brand-new channel is backfilled with everything currently in the feeds." Verified: on a cold `digest.sqlite3`, all 12 fixture items across 3 channels were sent; adding a new channel later to an already-populated db sent it the entire current backlog on its first run.

This is the top band verbatim: "All three forks identified and resolved explicitly."

### Failure-mode reasoning — 8 / 8

Per-channel marking, ordered after a successful send, with the tradeoff stated. `digest.py:52-68`:

```python
for chan_cfg in cfg["channels"]:
    name = chan_cfg["name"]
    selected = [
        i
        for i in all_items
        if matches(i, chan_cfg) and not store.was_delivered(db, name, i)
    ]
    if not selected:
        continue
    body = render.digest(selected, chan_cfg)
    if dry_run:
        print(f"--- would send to {name} ---")
        print(body)
        continue
    channels.send(chan_cfg, body)
    store.mark_delivered(db, name, selected)
    sent += len(selected)
```

preceded by the comment at `digest.py:45-50`:

> "Items are marked after a successful send only: a failed send aborts the run, and the next cron tick retries what never got through."

I reproduced the exact scenario the rubric asks about — channel two fails after channel one succeeds — by pointing a two-channel config at a dead webhook for the second channel. `ops` (stdout) sent and committed; `channels.send` then raised `DeliveryError` for `energy` (webhook), which propagated out of `main()` uncaught (unchanged pre-existing behaviour from `channels.py`'s own docstring: "we let the exception propagate and cron will pick us up again"). Inspecting the db afterward showed `ops`'s three rows committed in `delivered`, `energy`'s rows absent. Re-running on the next tick sent `ops` nothing (already delivered) and `energy` its full set (correctly retried). This is exactly "per-channel marking, correct ordering, tradeoff argued" (at-least-once, duplicates-over-drops, stated explicitly) — top band.

### Existing-code respect — 6 / 6

- `channels.py` and `render.py` are untouched (confirmed by diff).
- `feeds.py` gets only an added docstring paragraph (`feeds.py:11-13`) pointing at `store.item_key`; no functional change.
- `items` archive table and `record_items()` are untouched — the "unconditional insert" concern the rubric flags is handled correctly by *not* trying to dedupe through that table at all; dedupe lives in a new, additive `delivered` table instead.
- `--dry-run` still works as a preview path and the CLI surface (`--config`, `--db`, `--dry-run`) is unchanged.
- No scope creep — no scheduler, no plugin system, no new CLI flags.

### Code quality — 4 / 4

- `mark_delivered` uses `INSERT OR IGNORE` against a `(channel, source, link)` primary key (`store.py:93-97`), so double-marking the same item is a no-op rather than a crash or silent duplicate row — a clean idempotency choice.
- The new `CREATE TABLE IF NOT EXISTS delivered (...)` (`store.py:42-48`) is purely additive, so an existing production `digest.sqlite3` upgrades with zero migration effort — "schema migration handled for an existing database" is satisfied without any migration code being necessary.
- No dead code, no vestigial columns, parameterized SQL throughout, naming is consistent with the existing module style (`item_key`, `was_delivered`, `mark_delivered` mirror `record_items`/`count_items`).

### Documentation — 3 / 4

The README gains a "Choosing what goes in a digest" section (`README.md:18-36`) that covers dedupe semantics, the per-channel decision, backfill, and how to reset ("Deleting the db file (or its `delivered` table) resets everyone's memory") — hitting all three things the rubric asks for (dedupe behaviour, reset, first-run). This is well above "docstring alone."

However, one claim is simply false. `README.md:13-14`:

> "`--dry-run` prints what would be sent instead of sending it. It does not consult the delivery ledger, so it always shows the full current feed."

This does not match the code: `selected` in `digest.py:54-58` filters through `store.was_delivered(...)` *before* the `if dry_run:` branch is reached, so dry-run **does** consult the ledger (see Bugs below for a reproduction). A maintainer trusting this README line to debug "why does `--dry-run` show nothing" will be misled — which is exactly the failure mode this task's "leave it understood" instruction was meant to prevent. That costs a point.

## 3. What it missed

- **Unbounded ledger growth.** Neither `delivered` nor `items` is ever pruned; both grow forever under a 15-minute cron. Not required by the prompt, but a real production concern for a job explicitly described as running from cron indefinitely, and it isn't mentioned anywhere (not even as a known limitation).
- **Filter changes don't resurface items.** If someone edits a channel's `keywords` in `config.toml` to newly match an item that was already delivered to that channel under the old filter (e.g. it only matched a *different* channel before), it will never be (re)sent to the newly-matching channel, because the ledger is keyed by `(channel, source, link)` regardless of what filter was active when it got marked. This is a real, silent decision — arguably the right one — but it's never named, unlike the three forks that were.
- **No automated test added.** The agent manually reasoned through the fixtures (based on its comments referencing feed-specific quirks) but left no `test_*.py` or equivalent to pin the dedupe behaviour down for the next change. Not explicitly demanded by the prompt, but the prompt's own "leave it understood" framing would have been well served by one.
- **Concurrent/overlapping cron ticks** aren't discussed. SQLite's locking will serialize writers, but nothing calls this out, and a 15-minute-interval cron on a slow run could in principle overlap.

## 4. Bugs

**`--dry-run` does not do what the README says it does.**

Reproduction (fixtures/snapshot-a, fresh db):

```
$ cp config.example.toml config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3      # first run: backfills, "sent 12 items"
$ python3 digest.py --config config.toml --db digest.sqlite3      # second run: "sent 0 items" (ledger working)
$ python3 digest.py --config config.toml --db digest.sqlite3 --dry-run
sent 0 items
```

Per `README.md:13-14`, the third invocation should have printed "the full current feed" for every channel, since dry-run is documented as not consulting the ledger. It prints nothing, because `digest.py:54-58` filters `selected` through `store.was_delivered()` unconditionally, and the `if dry_run:` check at line 62 only gates whether `channels.send()` is called — it does not bypass the ledger filter. The code's actual behaviour (dry-run previews what would *actually* be sent, respecting the ledger) is arguably more useful than what's documented, but the documentation is wrong and should be corrected to match — or the filter should be skipped under `--dry-run` if that was the intended behaviour. Either way, the mismatch needs to be resolved before merge.

No other functional bugs found; the dedupe, per-channel marking, and failure/retry paths all behaved exactly as documented under direct testing.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 3,
  "total": 38.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid link-based dedupe ledger; README wrongly claims --dry-run ignores it."
}
```
