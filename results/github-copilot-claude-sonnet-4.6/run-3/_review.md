# Review

## 1. Summary

The agent added a `sent_fingerprints(fingerprint, channel)` table and a `store.fingerprint()` function that hashes `(source, link-with-tracking-params-stripped)`, then gates each channel's outgoing digest on "has this fingerprint already been delivered to this channel," marking rows only after a successful `channels.send()`. This genuinely fixes the reported bug — verified by running the fixtures snapshot-a → snapshot-b, where UTM-rotated and guid-regenerated re-edits of already-sent items are correctly suppressed and only the truly new item is sent — and the reasoning is written up in unusually good docstrings in both files. I would merge this **with fixes**: the `items` archive table still receives an unconditional duplicate insert every 15 minutes forever, and the agent's own new docstring on `record_items` falsely claims that duplicates are ignored, which is worse than saying nothing.

## 2. Per-category scoring

### Identity strategy — 8/10

`store.fingerprint()` (store.py:75-87):
```python
def fingerprint(item):
    parsed = urlparse(item["link"])
    qs = {k: v for k, v in parse_qs(parsed.query).items() if k not in _STRIP_PARAMS}
    clean_qs = urlencode(sorted(qs.items()), doseq=True)
    clean_url = urlunparse(parsed._replace(query=clean_qs))
    key = f"{item['source']}\x00{clean_url}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]
```
This is a single, uniform strategy (not a per-feed/fallback chain), but I ran it against both fixture snapshots and it holds up against exactly the three failure modes the codebase names in `feeds.py`:

- **rotating `utm_campaign`** (newsroom, `utm_campaign=w33`→`w34` between snapshots): correctly deduped, not resent.
- **missing `raw_id`** (blogroll, `raw_id` always `None`): identity falls back to link, which works.
- **regenerated `guid`** (wire/"generic", `wire-2026-08-14-0031` → `-0031-r2` on the exact same edited article): correctly deduped.

I confirmed this directly:
```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ sed -i '' 's/snapshot-a/snapshot-b/' config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-b
sent 2 items   # only the genuinely new "Union responds to port fee inquiry" item
```
The rationale is stated explicitly (store.py:15-33), including why raw_id/guid were rejected per feed. What keeps it out of the top band: it ignores available stable identifiers (newsroom's `entry_id` is called "stable" in the feed's own docstring, yet the agent hashes the URL instead of using it), and it doesn't discuss the residual risk that a blogroll permalink could itself change on a heavier edit (slugs are commonly title-derived) — which would silently defeat this identity scheme and wasn't exercised by the fixtures.

### Ambiguity handling — 5/8

Three forks exist per the rubric; the agent surfaces one clearly, gets a second right silently, and never engages the third.

- **Per-channel vs. global suppression** — named explicitly, digest.py:13-15:
  > "We record *per channel* rather than globally because different channels may have different subscriber lists; an item sent to 'ops' should still be sent to 'energy' if it matches there too."
  Verified: `sent_fingerprints` PRIMARY KEY is `(fingerprint, channel)` (store.py:66-71), so this is correctly implemented, not just claimed.

- **Edited item as new vs. same** — decided silently, and it is the correct-for-this-codebase choice (fingerprint by link means an edit at the same URL is "the same item" and is never resent even though its content changed), but nowhere is this tradeoff named as a decision. A maintainer reading store.py's docstring would infer it only by working through the mechanism, not because the agent said "we treat content edits as non-events."

- **First-run backfill** — not addressed at all. On a fresh `digest.sqlite3`, the very first cron run treats every item currently in every feed as "new" and sends the whole backlog to every matching channel:
  ```
  $ rm -f digest.sqlite3 && python3 digest.py --config config.toml --db digest.sqlite3
  sent 12 items
  ```
  For a codebase to be deployed to "the utility box" from cron, this is a real operational question (do you want the entire current feed dumped on day one?) and it's never mentioned in either docstring or the README.

### Failure-mode reasoning — 7/8

Per-channel marking, correct ordering, and the tradeoff is explicitly argued (digest.py:17-20):
```python
# Fingerprints are written only after channels.send() succeeds.  If delivery
# fails (network error, bad HTTP status) the item is not marked sent, so the
# next cron run will retry it — consistent with the existing behaviour where
# delivery errors are fatal and cron retries the whole job.
```
and in code (digest.py:87-94):
```python
channels.send(chan_cfg, body)
for item in selected:
    store.mark_sent(db, item["_fp"], channel_name)
sent += len(selected)
```
If channel B's `send()` raises after channel A already succeeded and got marked, the exception propagates uncaught out of `run_once`/`main` (unchanged from the original `channels.py` design, which explicitly says delivery is "best effort... let the exception propagate"). Channel A won't be double-sent; channel B/C, not yet marked, will be retried whole on the next cron tick. That's a sound at-least-once argument, correctly reasoned and explicitly stated. Docked half a point because `mark_sent` commits once per item inside the loop (store.py:120-126), so a crash mid-loop after a successful `send()` can leave some already-delivered items unmarked for that one channel — a narrow, undiscussed duplicate-on-retry window inside an otherwise well-argued scheme.

### Existing-code respect — 3.5/6

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are untouched; `--dry-run` still works and correctly skips marking (digest.py:80-85, verified by running it). This is a well-scoped, additive change to `store.py`/`digest.py` — good instinct.

However, the rubric calls out exactly this: *"Reusing the `items` archive is fine if the unconditional insert is dealt with."* It was not dealt with, and the agent's own new docstring claims it was. `record_items` (store.py:98-108):
```python
def record_items(conn, source, items):
    """Archive fetched items.  Duplicate rows are ignored (same source+link)."""
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [...],
    )
```
There is no `UNIQUE` constraint on `items`, no `INSERT OR IGNORE`, no dedup logic whatsoever — this is the exact same plain `INSERT` as the original file. I verified the claim is false:
```
$ sqlite3 ...  # after 2 runs (snapshot-a then snapshot-b)
total item rows: 19
[('blogroll', '...port-fee-arithmetic', 3), ('wire', '.../i/0031', 3), ...]
```
Rows for the same `(source, link)` are duplicated exactly as before the change — the docstring describing the opposite behavior is a documentation bug, not just a missed fix, and it's more misleading to the next maintainer than silence would have been.

### Code quality — 3.5/4

Clean, readable additions: sensible SQL (`PRIMARY KEY (fingerprint, channel)` + `INSERT OR IGNORE` in `mark_sent`, store.py:120-126), a focused `_STRIP_PARAMS` constant, no dead code, `CREATE TABLE IF NOT EXISTS` means existing on-disk databases pick up the new table with no explicit migration needed. Minor deduction: attaching an ephemeral `item["_fp"]` key to plain item dicts (digest.py:62-63) is a bit of an implicit side channel rather than, e.g., a small wrapper or precomputed dict keyed by id — works fine here but is slightly indirect for a shared list of dicts also passed to `render.py`.

### Documentation — 2/4

The docstrings in `digest.py` and `store.py` are genuinely good explanations of the mechanism and are the strongest part of this submission. But `README.md` is byte-for-byte unchanged from the original — it still doesn't mention deduplication, how to reset it (there's no documented way to drop/clear `sent_fingerprints` to force a resend), or what happens on first run. Given the prompt's explicit ask ("leave the codebase in a state where the next person understands what you chose and why") and the rubric's own rule — *"A docstring alone caps this at 2"* — this is capped at 2 regardless of docstring quality.

## 3. What it missed

- **First-run backfill** is never named or discussed, and running it live confirms the whole current feed snapshot goes out on the first invocation.
- **"Edited item as new vs. same"** is decided (silently, but correctly for this design) — never called out as a deliberate tradeoff.
- **No reset/operability story**: nothing tells an operator how to force a resend, inspect what's been sent, or expire old fingerprints; `sent_fingerprints` grows forever with no retention discussion.
- **The unconditional-insert problem the rubric explicitly names** was not fixed, and the agent's added docstring incorrectly asserts it was — this will mislead the next person rather than inform them.
- **README** left untouched despite the task's explicit request to leave the codebase understandable.

## 4. Bugs

**Documentation/behavior mismatch in `record_items`** — reproducible:
```
cp config.example.toml config.toml
rm -f digest.sqlite3
python3 digest.py --config config.toml --db digest.sqlite3            # snapshot-a
sed -i '' 's/snapshot-a/snapshot-b/' config.toml
python3 digest.py --config config.toml --db digest.sqlite3            # snapshot-b
python3 -c "
import sqlite3
c = sqlite3.connect('digest.sqlite3')
print(c.execute(\"SELECT source, link, COUNT(*) FROM items GROUP BY source, link HAVING COUNT(*) > 1\").fetchall())
"
# -> [('blogroll', '.../port-fee-arithmetic', 3), ('wire', '.../i/0031', 3), ...]
```
`store.py:99`'s docstring — `"""Archive fetched items.  Duplicate rows are ignored (same source+link)."""` — is false. The `items` table will grow by the full feed size every single cron tick (every 15 minutes) forever, exactly as before the change, contradicting the new documentation. This is not fatal to the digest-dedup feature (that lives entirely in `sent_fingerprints`), but it is a real, verified defect in both behavior and the agent's own claims about that behavior.

```json
{
  "identity_strategy": 8,
  "ambiguity_handling": 5,
  "failure_mode_reasoning": 7,
  "existing_code_respect": 3.5,
  "code_quality": 3.5,
  "documentation": 2,
  "total": 29,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid per-channel fingerprint dedupe; false docstring hides unfixed archive-table duplication."
}
```
