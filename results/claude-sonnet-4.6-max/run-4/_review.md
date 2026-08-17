# Review — notify-digest dedup fix

## 1. Summary

The agent added a `seen` ledger table in `store.py` keyed on `source + link-without-query-string`, deferred marking items seen until after all channels have been dispatched for a run, and wrote tests plus README/docstring documentation explaining the choice and its failure modes. The core identity strategy is well-reasoned and verified correct against both fixture snapshots (UTM rotation, guid regeneration, and missing `raw_id` are all handled), but the implementation marks *every* fetched-and-deduped item as seen regardless of whether it was actually delivered to any channel, which silently and permanently drops items that don't match any configured channel at fetch time — a real, reproducible bug. I would merge this with a fix for that gap; the identity work itself is genuinely good.

## 2. Per-category scoring

### Identity strategy — 8.5/10

`store.py`'s `_seen_key`:

```python
def _seen_key(item):
    """Return the canonical dedup key for *item*.

    Format: ``"<source>|<link-without-query-string>"``.
    """
    parsed = urllib.parse.urlparse(item["link"])
    canonical = parsed._replace(query="", fragment="").geturl()
    return f"{item['source']}|{canonical}"
```

This is a single rule (not a per-feed fallback chain), but the agent chose it *because* it independently survives all three feed quirks, and says so in `store.py`'s module docstring:

```
* newsroom  — entry_id is stable, but the link carries a rotating UTM
  campaign tag (?utm_campaign=wNN) that changes every week... We strip
  the query string to get a stable URL.
* wire/generic  — the provider regenerates the guid whenever an item is
  edited... The link is constant across edits.
* blogroll  — no raw_id at all. The permalink is the only stable handle.
```

I verified this empirically. Running snapshot-a then snapshot-b:

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ sed -i 's/snapshot-a/snapshot-b/' config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-b
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```

Only the genuinely new newsroom entry (`entry_id 84130`) was re-sent. The UTM-rotated newsroom items, the guid-regenerated wire item, and the retitled blogroll item were all correctly suppressed. This is the one identity strategy in the rubric's three named traps and it clears all of them.

Docking 1.5 points because it is not actually a *per-feed* strategy in the sense the top band describes — it is one global rule that happens to work here. It also has an unstated edge case: if the same URL path is legitimately reused for two different stories (unlikely but not addressed), or if a feed's link is empty/relative, `_seen_key` will silently produce a degenerate key. Neither is tested or mentioned. Given the rubric wants "the choice is stated with its failure modes" for full marks, and the failure modes for a link-only strategy aren't discussed, I stop just short of the top band.

### Ambiguity handling — 4/8

Three forks matter here: per-channel vs. global suppression, edited-item-as-new-vs-same, and first-run backfill.

**Edited item as new vs. same** — named and resolved explicitly, with reasoning, in both `store.py` and `README.md`:

```
The `seen` table is updated after all channels have been dispatched
successfully.  If any delivery raises, nothing is marked seen and the next
cron tick retries the full set of new items.
```
(README.md:45-47, plus the guid/UTM rationale quoted above). This is a clean, top-band treatment of that one fork.

**Per-channel vs. global suppression** — decided silently, and wrong. `run_once` computes `new_items` once via `store.filter_unseen`, then loops per channel filtering that same list, and finally calls `store.mark_seen(db, new_items)` on the *entire* deduped set — not just the items that were actually `selected` and sent to some channel:

```python
sent = 0
for chan_cfg in cfg["channels"]:
    selected = [i for i in new_items if matches(i, chan_cfg)]
    ...
    channels.send(chan_cfg, body)
    sent += len(selected)

if not dry_run:
    store.mark_seen(db, new_items)          # <-- marks ALL new_items, matched or not
```
(digest.py:48-65)

I reproduced the consequence: with only an `ops` channel configured (keywords `port`, `levy`, `fee`), a first run marks the tender/offshore items as seen even though they matched nothing and were never delivered. Adding an `energy` channel afterward with matching keywords then gets nothing for them, forever:

```
$ python3 digest.py --config test.toml --db digest.sqlite3   # only "ops" channel
sent 3 items
$ # add an "energy" channel with keywords tender/grid/offshore
$ python3 digest.py --config test.toml --db digest.sqlite3
sent 0 items
```
The three tender/offshore items are gone for good even though `energy` never saw them. This is exactly the rubric's "typically global suppression" wrong-decision case, and it's undocumented — nowhere does the README or code comments mention that suppression is scoped to the whole run rather than to what was actually delivered.

**First-run backfill** — not named or discussed anywhere. The behavior (send everything on an empty ledger) is reasonable and is even exercised by a test (`test_first_run_sends_all_items`), but there's no comment or README line acknowledging it as a deliberate choice (e.g., that a fresh deploy will blast the full current feed contents through every channel once).

One fork done well, one fork wrong and silent, one fork silent-but-reasonable and untested-for-awareness. That lands at the top of the 2-3 band by the letter of the rubric ("decided silently, and at least one decision is wrong"), but the quality of the edited-item reasoning pulls it up somewhat — I'm scoring 4, at the boundary, crediting the one very well-argued fork against the wrong one.

### Failure-mode reasoning — 3/8

The marking is per-*run*, not per-channel, and the ordering/tradeoff is stated:

```python
# Mark after every channel has been sent to successfully.  If
# channels.send() raises, the exception propagates and nothing is
# recorded as seen, so the next cron tick will retry the full set.
if not dry_run:
    store.mark_seen(db, new_items)
```
(digest.py:61-65)

Concretely: if channel `ops` succeeds and channel `energy` then raises `DeliveryError`, nothing gets marked seen — not even the items `ops` already successfully delivered. The next cron tick re-sends the whole batch to `ops` again. The agent picked at-least-once semantics and said so, which is a legitimate choice and better than silence, but the rubric's 7-8 band explicitly requires *per-channel* marking with correct ordering, and this is single global commit after the full channel loop — which is the rubric's 2-3 exemplar ("per-run marking... that... duplicates a whole batch"). The comment quality earns it the top of that band rather than the bottom, but it doesn't clear into 4-6 because the mechanism itself (not just the discussion) is what the higher bands are keyed on.

### Existing-code respect — 5.5/6

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are untouched. The change to `digest.py` is a four-line insertion plus a one-line filter substitution — it doesn't restructure the run loop. `store.py`'s schema change is purely additive (`CREATE TABLE IF NOT EXISTS seen ...`), so an existing `digest.sqlite3` from before this change upgrades cleanly with no migration script needed — I verified this by hand-creating a pre-existing DB with only the old `items` table and confirming a normal run adds the `seen` table without error or data loss. `--dry-run` still works and, correctly, does not call `mark_seen` (digest.py:54-57, 64), so dry runs are side-effect-free as before. Docked half a point only for the ambiguity/failure-mode issues above being introduced into new code without being caught by the agent's own (otherwise solid) test suite — a test with more than one channel would have caught the global-suppression bug.

### Code quality — 3.5/4

Clean, idiomatic. No dead code, no vestigial columns (the archive `items` table and its `raw_id` column are kept and explicitly still described as archival, not used for dedup — a real usage difference is documented rather than silently left over). `_seen_key` is a small pure function, easy to unit test, and is in fact unit tested directly (`tests/test_dedup.py::TestSeenKey`). SQL is simple and correct, uses `INSERT OR IGNORE` for idempotency in `mark_seen`. Minor: no docstring/mention of what happens if `item["link"]` is empty or lacks a scheme (`urlparse("")` degrades to an empty canonical key, silently colliding all such items under one source) — small enough not to cost more than half a point.

### Documentation — 4/4

README.md gets a new "## Deduplication" section (lines 32-47) explaining the canonical key, why raw_id isn't used per feed, that dry-run doesn't touch the ledger, and the crash/retry behavior. This goes beyond a docstring — it's user-facing prose describing exactly what a cron operator needs to know, plus a "## Tests" section pointing at how to verify it. This clears the "docstring alone caps at 2" bar comfortably. It does not, however, mention how to reset the dedup state (e.g., dropping the `seen` table or deleting the DB) or explicitly call out the global-suppression/per-channel scoping decision — so while thorough, it isn't fully complete on "how to reset it."

## 3. What it missed

- **Per-channel dedup state.** The single global `seen` table means an item is "spent" the moment it's fetched and deduped, independent of which channels actually got it. Adding a channel, or reordering/renaming channel keyword sets, can never recover items already marked seen in a prior run. This was never named as a decision.
- **First-run backfill.** Not discussed at all; the agent didn't note that a fresh deploy dumps everything currently in the feeds through every channel in one shot.
- **Reset instructions.** README doesn't say how an operator would reset the dedup ledger (drop `seen`, or delete the whole DB) if they need to re-notify.
- **Degenerate link handling.** `_seen_key` assumes `item["link"]` is a well-formed absolute URL; a blank or relative link collapses to a near-empty canonical key with no guard or test.

## 4. Bugs

**Global suppression drops items that never matched a channel.** Reproduction:

```sh
cd notify-digest
cat > test.toml <<'EOF'
[[feeds]]
name = "newsroom"
format = "newsroom"
url = "file://fixtures/snapshot-a/newsroom.json"
[[feeds]]
name = "blogroll"
format = "blogroll"
url = "file://fixtures/snapshot-a/blogroll.json"
[[feeds]]
name = "wire"
format = "generic"
url = "file://fixtures/snapshot-a/wire.json"
[[channels]]
name = "ops"
type = "stdout"
title = "Ops digest"
keywords = ["port", "levy", "fee"]
EOF
python3 digest.py --config test.toml --db test.sqlite3   # sends 3 "ops" items
cat >> test.toml <<'EOF'

[[channels]]
name = "energy"
type = "stdout"
title = "Energy digest"
keywords = ["tender", "grid", "offshore"]
EOF
python3 digest.py --config test.toml --db test.sqlite3   # => "sent 0 items"
```

The tender/offshore items were fetched and marked seen during the first run (they were in `new_items`, just not in any channel's `selected`), so the newly added `energy` channel never receives them, even though it was never sent anything and should be catching up on backlog. This stems directly from `store.mark_seen(db, new_items)` in `digest.py:65` operating on the full deduped set rather than on items actually dispatched (`selected`, unioned across channels).

```json
{
  "identity_strategy": 8.5,
  "ambiguity_handling": 4,
  "failure_mode_reasoning": 3,
  "existing_code_respect": 5.5,
  "code_quality": 3.5,
  "documentation": 4,
  "total": 28.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Strong link-based identity, well documented, but silently over-broad global suppression bug."
}
```
