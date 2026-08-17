# Review: notify-digest dedup fix (run-2)

## 1. Summary

The agent added a `sent` table keyed on `(fingerprint, channel)`, where the fingerprint is the item's link with query string and fragment stripped, and wired it into `run_once` so each channel only receives items it hasn't already been sent. This correctly defeats all three fixture-specific traps — rotating `utm_campaign` on the newsroom feed, a missing `raw_id` on blogroll, and a regenerating `guid` on wire — verified by running snapshot-a then snapshot-b against the same database. It's a small, surgical, well-reasoned diff that doesn't touch `feeds.py`, `channels.py`, or `render.py`, and I would merge it, though the missing README update and unargued failure-mode tradeoff are worth a follow-up.

## 2. Per-category scoring

### Identity strategy — 9/10

The strategy is a single normalized key — link URL with query and fragment stripped — applied uniformly, with the choice justified per-feed in the `store.py` docstring:

```python
# store.py:13-25
Why fingerprint on link URL (query string stripped)?
----------------------------------------------------
Each feed format has a different reliability profile for identifiers:

*   **newsroom** — stable ``entry_id``, but the feed URL carries UTM params
    that rotate between polls (``utm_campaign=w33`` -> ``w34``).
*   **blogroll** — no identifier at all (``raw_id is None``); the permalink
    is stable.
*   **generic / wire** — has a ``guid``, but the provider regenerates it on
    every edit.  The link itself is stable.

The link (minus query string and fragment) is the only value that is both
present and stable across all three formats.
```

```python
# store.py:66-73
def fingerprint(link):
    """Normalise a link URL into a dedup key.
    Strips query string and fragment so that tracking parameters (e.g. UTM
    tags that rotate every poll cycle) do not defeat deduplication.
    """
    p = urlparse(link)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
```

I verified this against both fixture snapshots. First run against snapshot-a sends all 12 (channel, item) pairs. Running again against the identical snapshot sends 0. Switching to snapshot-b (UTM rotated `w33`→`w34` on newsroom, `guid` regenerated on wire, title/excerpt edited on blogroll, one genuinely new item added) against the *same* database sends exactly the 2 new-item deliveries (`ops` and `everything`, matching the new "Union responds to port fee inquiry" story) and nothing else — the edited/rotated items are correctly suppressed as duplicates.

Docked one point because it doesn't use `raw_id` at all even where available (e.g. newsroom's stable `entry_id`) — a fallback chain (`raw_id` when present, else normalized link) would be more robust against a link changing for the same story (e.g. a slug correction), which the docstring's own risk analysis doesn't cover. It's a single strategy, not a fallback chain, but it does hold up against everything the fixtures throw at it and states its reasoning, so it sits at the top of the "single strategy that works for most items" territory and close to the top band.

### Ambiguity handling — 6/8

Two of the three forks are explicitly named and reasoned about:

- **Per-channel vs. global suppression** — resolved as per-channel, visible directly in the schema (`PRIMARY KEY (fingerprint, channel)`, store.py:45-50) and confirmed by testing: the `energy` channel (whose keyword filter didn't match the new story) correctly did not fire on the snapshot-b run while `ops` did.
- **Edited item as new vs. same** — explicitly decided and stated ("The link itself is stable" / dedup is on link only, ignoring title/summary changes) — verified: the blogroll post whose title changed to "Notes on port fee arithmetic (updated)" and the wire item whose description changed were both suppressed as duplicates, exactly as the docstring predicts.

The third fork — **first-run backfill** — is decided silently. On an empty `sent` table (first run, or a fresh deploy), every matching item is sent immediately; nothing in `store.py`, `digest.py`, or `README.md` flags that this is the chosen behavior, or that it differs from "suppress everything on the first run and only alert going forward" (which many teams prefer to avoid a flood on deploy day). I confirmed this from a clean DB: first run against snapshot-a sent all 12 items outright. The decision happens to be reasonable, but it isn't surfaced.

### Failure-mode reasoning — 6/8

Marking is per-channel and ordered correctly — write only happens after a successful send, and it commits before moving to the next channel:

```python
# digest.py:56-59
channels.send(chan_cfg, body)
store.mark_sent(db, chan_cfg["name"], selected)
sent += len(selected)
```

```python
# store.py:94-100
def mark_sent(conn, channel, items):
    """Record that *items* have been delivered to *channel*."""
    conn.executemany(
        "INSERT OR IGNORE INTO sent (fingerprint, channel) VALUES (?, ?)",
        [(fingerprint(i["link"]), channel) for i in items],
    )
    conn.commit()
```

I simulated a mid-run failure (monkeypatched `channels.send` to raise on the third channel after the first two succeeded). Result: `ops` and `energy` were correctly marked sent (each committed independently right after its own delivery), the exception from the third channel propagated up through `run_once`/`main` uncaught, and the third channel's items were *not* marked — so the next cron tick will retry only the channel that actually failed, not resend to the channels that already got their digest. This is the right behavior and matches `channels.py`'s existing "let it propagate, cron retries" contract.

What's missing is any argument for *why* this is at-least-once (a channel could theoretically receive the same item twice if it succeeds but the process is killed between `channels.send` returning and `mark_sent`'s commit) rather than the reverse. Nothing in the diff states the tradeoff explicitly — it's implemented correctly but not argued, which is exactly the "sane ordering, no discussion" band.

### Existing-code respect — 6/6

The change is minimal and additive. `feeds.py`, `channels.py`, and `render.py` are untouched (confirmed via `diff`, all three are byte-identical to the original). The pre-existing `items` archive table and its unconditional insert in `record_items` are left exactly as they were — the agent added a *second*, purpose-built table (`sent`) rather than repurposing the archive or bolting dedup logic onto it, which keeps the archive's stated "write-only, nothing reads it" contract true. Schema evolution for an existing on-disk database is handled correctly since `CREATE TABLE IF NOT EXISTS sent (...)` inside the existing `SCHEMA` script means an old `digest.sqlite3` picks up the new table on the next `connect()` with no migration step needed. `--dry-run` was explicitly preserved and even reasoned about:

```python
# digest.py:51-56
body = render.digest(selected, chan_cfg)
if dry_run:
    print(f"--- would send to {chan_cfg['name']} ---")
    print(body)
    # Don't mark as sent so real runs still deliver these items.
    continue
```

I verified this: a `--dry-run` invocation followed by a real invocation against the same DB delivered the identical 12 items on the real run — dry-run doesn't silently consume the "sent" state.

### Code quality — 4/4

The new code is short, readable, and has no dead code or vestigial columns. The `unsent_items` query batches with a single `IN (...)` rather than one query per item:

```python
# store.py:76-91
def unsent_items(conn, channel, items):
    """Return the subset of *items* not yet sent to *channel*."""
    if not items:
        return []
    fps = [fingerprint(i["link"]) for i in items]
    unique_fps = list(set(fps))
    placeholders = ",".join("?" * len(unique_fps))
    already = {
        row["fingerprint"]
        for row in conn.execute(
            f"SELECT fingerprint FROM sent"
            f" WHERE channel = ? AND fingerprint IN ({placeholders})",
            [channel] + unique_fps,
        )
    }
    return [i for i, fp in zip(items, fps) if fp not in already]
```
Parameterization is correct (values go through `?` placeholders, not string-interpolated into the SQL), so despite the f-string this isn't an injection risk — only the placeholder count is interpolated. Schema migration for existing databases is handled by `CREATE TABLE IF NOT EXISTS`, requiring no separate migration script.

### Documentation — 2/4

The reasoning is real and good, but it lives entirely in the `store.py` module docstring — the rubric caps a docstring-only writeup at 2, and I checked: `README.md` is byte-identical to the original (confirmed via `diff`), and the "Layout" table still says `store.py` is "SQLite archive of everything seen" with no mention that it now also does delivery-dedup:

```
| `store.py` | SQLite archive of everything seen |
```

Nothing tells an operator how to reset the dedup state (e.g., to force a resend, or to recover after wiping the wrong channel's history), and there's no mention anywhere of the first-run-sends-everything behavior. A future reader has to go into `store.py` to discover that dedup exists at all — the README, which is the file most likely to be read by "the next person to touch it," doesn't mention it.

## 3. What it missed

- **README never updated.** The rubric explicitly asks for dedupe behavior, reset instructions, and first-run behavior in the README; none of that happened. The `Layout` table description of `store.py` is now stale.
- **First-run backfill fork not named anywhere**, even though it's a real, consequential decision (a fresh deploy or a wiped DB sends every currently-matching item as if brand new).
- **No reset mechanism** documented or provided (no CLI flag, no note on which table/rows to delete to re-arm a channel).
- **At-least-once vs. at-most-once tradeoff for failure semantics is never argued**, even though the implementation happens to make the right call (commit-per-channel, exception propagates for cron retry).
- Doesn't exploit `raw_id` even where the feed does supply a stable one (newsroom's `entry_id`); relies solely on link stability across all three feeds. This happens to work with the given fixtures but is a narrower net than a fallback chain would be.

## 4. Bugs

None found. Specifically checked and did not find:
- Duplicate items table (archive) does not silently start filtering — `record_items` is untouched and still archives one row per item per run, which is the documented, load-bearing behavior of that table.
- Cross-feed fingerprint collisions: not tested by the fixtures (no two feeds share a link), and not a bug per se, just an inherent property of the chosen link-scoped strategy (fingerprint is not source-scoped, so two different feeds independently linking to the same URL would collide — this is a plausible but unaddressed edge case, not something I could reproduce as broken with the given fixtures).
- `--dry-run` + real run interaction verified correct (see Existing-code respect section) — no double-send or item loss.
- Partial-failure mid-run simulation (patched channel 3 of 3 to raise): channels 1–2 correctly retained their "sent" marks, channel 3 correctly did not, and the exception surfaced for cron to retry — no batch-wide loss or duplication.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 6,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 2,
  "total": 33,
  "would_merge": "yes-with-fixes",
  "one_line": "Correct link-fingerprint per-channel dedup, verified against fixtures; README never updated."
}
```
