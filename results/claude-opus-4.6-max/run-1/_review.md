# Review — claude-opus-4.6-max, run-1

## 1. Summary

The agent added a `sent_items` table and a per-channel `is_already_sent`/`mark_sent` pair to `store.py`, and wired the check/mark into `digest.py`'s send loop, using an OR-of-`raw_id`-or-`link` identity check that is documented (in a `store.py` docstring only) as deliberately covering all three feed formats' quirks. It verifiably stops repeats across both fixture snapshots, including edited items, and does not touch `--dry-run`, `feeds.py`, `channels.py`, or `render.py`. I would merge this with fixes: the mechanism is correct and tested-out, but the agent never touched `README.md`, so the "leave it understandable" half of the prompt is unmet, and two of the three classic ambiguities (backfill-on-deploy, per-channel vs. global) are resolved correctly in code but never named anywhere.

## 2. Per-category scoring

### Identity strategy — 9/10

`store.py:82-101` (`is_already_sent`) checks link first, then falls back to `raw_id`:

```python
def is_already_sent(conn, channel, item):
    """Return True if this item was previously delivered to this channel."""
    # Check by link (universally available and stable for wire + blogroll).
    row = conn.execute(
        "SELECT 1 FROM sent_items WHERE channel = ? AND source = ? AND link = ?",
        (channel, item["source"], item["link"]),
    ).fetchone()
    if row:
        return True
    # Check by raw_id (stable for newsroom; covers the case where the link
    # gains new tracking params between runs).
    if item.get("raw_id"):
        row = conn.execute(
            "SELECT 1 FROM sent_items"
            " WHERE channel = ? AND source = ? AND raw_id = ?",
            (channel, item["source"], item["raw_id"]),
        ).fetchone()
        if row:
            return True
    return False
```

and the docstring at `store.py:9-27` explicitly maps each of the three feeds to which signal saves it (newsroom → `raw_id` survives `utm_campaign` rotation; wire → `link` survives guid regeneration on edit; blogroll → `link` is the only signal it has). I ran it for real: `snapshot-a` then `snapshot-a` again sends 0 the second time; `snapshot-a` then `snapshot-b` sends only the one genuinely-new newsroom item and correctly suppresses the edited blogroll post (new excerpt, same permalink), the edited wire item (new guid `wire-2026-08-14-0031-r2`, same link), and the newsroom item whose link rotated its `utm_campaign` from `w33` to `w34`. This is exactly the 9–10 band description ("survives rotating utm_campaign, a missing raw_id, and a regenerated guid"). Docked half a point because the docstring states the per-feed rationale but not the failure mode of the OR itself — e.g. a `raw_id` collision between two distinct items on the same source would falsely suppress one, and that risk isn't acknowledged.

### Ambiguity handling — 5/8

Three forks apply here; the agent's code decides all three correctly but only documents one of them, and even that one is documented as an identity-mechanism side effect rather than as a named decision.

- **Per-channel vs. global suppression**: resolved correctly — `channel` is part of every key in `sent_items` (`store.py:46-58`, `82`, `104`), so an item suppressed for `ops` can still reach `energy`. But this choice is never stated anywhere as a choice; nothing says "we track sent-state per channel, not globally, because channels have independent keyword filters." It's inferable from the schema, not explained.
- **Edited item = same vs. new**: implicitly discussed via the identity rationale quoted above ("the guid is regenerated on every edit, but the link is stable → link catches the duplicate") — this is the closest thing to a named fork in the diff, though it's framed as an identity detail, not as "we chose to treat corrections/edits as non-notifying."
- **First-run backfill**: not mentioned at all. Confirmed by running: a fresh `digest.sqlite3` (or an existing production DB being migrated onto this code, which is the actual deployment scenario per the task) has an empty `sent_items` table, so the very next run — no matter how many times those items have already gone out under the old code — resends everything currently live in the feeds once. That's a defensible default, but it's exactly the kind of decision the rubric wants surfaced and it is not, in code or README.

### Failure-mode reasoning — 5/8

The ordering is correct — mark only after successful delivery, per channel — but never argued. `digest.py:46-61`:

```python
for chan_cfg in cfg["channels"]:
    selected = [
        i for i in all_items
        if matches(i, chan_cfg)
        and not store.is_already_sent(db, chan_cfg["name"], i)
    ]
    if not selected:
        continue
    body = render.digest(selected, chan_cfg)
    if dry_run:
        print(f"--- would send to {chan_cfg['name']} ---")
        print(body)
        continue
    channels.send(chan_cfg, body)
    store.mark_sent(db, chan_cfg["name"], selected)
    sent += len(selected)
```

I reproduced the partial-failure case directly: two channels, `ops` (stdout) and `broken` (webhook to a refused port). `channels.send` raises `DeliveryError` for `broken`, which propagates uncaught out of `run_once`/`main` and kills the process with a traceback (exit code 1). Checking `sent_items` afterward: `[('ops', 2)]` — `ops` was marked and will not re-notify on the next cron tick; `broken` was never marked and will correctly retry with the same items in 15 minutes. That is the right at-least-once behavior for the failed channel and the right at-most-once behavior for the one that already succeeded. But `grep -i "crash|retry|at-least|partial|failure"` over every `.py` file and the README returns nothing — the tradeoff is never argued, and the uncaught traceback/exit-1-on-one-bad-webhook behavior (cron will just see a nonzero exit and the log fills with a stack trace every 15 minutes until someone notices) is left as-is with no comment about whether that's intended.

### Existing-code respect — 6/6

Diff touches exactly two files, `digest.py` and `store.py`; `feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are byte-identical to the original. The pre-existing `items` archive insert (`store.record_items`, unchanged) is left as an unconditional per-run append, which is fine since it was already documented in the original `store.py` as "an archive of everything we have ever seen" and nothing about the new dedup logic depends on or corrupts it. `--dry-run` still prints without touching `sent_items` — confirmed by running twice with `--dry-run` and seeing full output both times, then a real run afterward still sending everything. Schema change is additive (`CREATE TABLE IF NOT EXISTS sent_items ...`), so an existing production `digest.sqlite3` (with its accumulated `items` archive) loads cleanly without a migration script.

### Code quality — 3/4

Readable, no dead code, indices added for the new lookups (`store.py:55-58`). One real smell: `is_already_sent` issues up to two separate round-trip `SELECT`s per item per channel (`store.py:85-100`), inside a Python-level list comprehension in `digest.py:47-51` — so for N items and M channels this is up to 2×N×M individual queries instead of one batched lookup (e.g. pre-loading each channel's sent set once, or a single `link = ? OR raw_id = ?` query). At fixture scale (a handful of items) this is invisible; it's a design habit that won't scale past a few feeds without becoming the new bottleneck, and it's an easy fix that wasn't done.

### Documentation — 2/4

`README.md` is untouched — `diff` against the original is empty. There is no mention anywhere in the README of dedupe behavior, how to reset it (e.g. clearing `sent_items` to force a resend), or what happens on first run / after a schema migration. All of the (genuinely good) reasoning lives in the `store.py` module docstring quoted above. Per the rubric, "a docstring alone caps this at 2," so this is capped regardless of the docstring's quality.

## 3. What it missed

- **README left completely unedited.** The prompt explicitly says "leave the codebase in a state where the next person to touch it understands what you chose and why" — the next person has to go read `store.py`'s docstring to learn any of this; the README (which has a file-purpose table and a deployment section) says nothing.
- **First-run / migration blast not addressed or mentioned.** Deploying this onto the existing production `digest.sqlite3` will resend every currently-live item to every channel once, silently, with no note about that being expected or how to avoid it (e.g., seeding `sent_items` from `items` on migration).
- **Per-channel vs. global suppression decided correctly but never named as a decision.** It's inferable from the schema but not called out anywhere as a considered choice with an alternative.
- **Crash/retry tradeoff never argued**, even though the code's actual behavior (per-channel marking, uncaught exception on delivery failure) happens to be the sane choice — it's not stated as a choice.
- **No batching of the sent-check**, so the per-item, per-channel query pattern is left as a latent scaling cost.

## 4. Bugs

None found. The mechanism was exercised directly:
- `snapshot-a` → `snapshot-a`: second run sends 0 items (correct suppression).
- `snapshot-a` → `snapshot-b`: only the genuinely new newsroom item is sent; the edited blogroll post, the guid-rotated wire item, and the utm-rotated newsroom item are all correctly suppressed.
- `--dry-run` does not call `mark_sent`, confirmed by a subsequent real run still sending the full set.
- A simulated webhook failure on one channel leaves that channel unmarked (will retry) while a prior successful channel in the same run is marked (won't duplicate) — this is the correct ordering, just not documented as intentional.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 5,
  "failure_mode_reasoning": 5,
  "existing_code_respect": 6,
  "code_quality": 3,
  "documentation": 2,
  "total": 30,
  "would_merge": "yes-with-fixes",
  "one_line": "Correct per-channel dedupe, verified across fixtures, but README untouched and tradeoffs unargued."
}
```
