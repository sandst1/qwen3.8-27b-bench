# Review — qwen3.8-27b-nvfp4-nothink, run-3

## 1. Summary

The agent introduced a normalised-link identity (stripping `utm_*` params) and a `UNIQUE` index on `items.link`, changed `record_items` to return only genuinely-new items, and wrote a real "Deduplication" section in the README explaining the choice, the reset behaviour, and the first-run/`--dry-run` gotchas. The identity strategy itself is well-reasoned and verified correct against both fixture snapshots, but the implementation marks items as "seen" in the database *before* they are actually delivered to any channel, so any channel-delivery failure (which `channels.py` explicitly documents as an expected, retryable condition) now causes silent, permanent loss of the whole batch — a regression the original bug (duplicate sends) never had. I would merge this with fixes: the identity work and docs are good, but the delivery-ordering bug must be fixed before it goes near cron.

## 2. Per-category scoring

### Identity strategy — 9 / 10

The chosen identity is the *normalised* link, with `raw_id` explicitly demoted to "provenance only":

```python
# feeds.py
def normalise_link(url):
    """Strip utm_* query params so links stay stable across polls.

    The newsroom feed rotates utm_campaign every poll; without this the
    same story would look like a new item each time.
    """
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in query if not k.lower().startswith("utm_")]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), parts.fragment)
    )
```

```python
# store.py
"""
Identity is the normalised link, not `raw_id`:

- the blogroll feed has no identifier at all;
- the wire feed regenerates its guid whenever an item is edited;
- newsroom URLs differ between polls (rotating utm_campaign parameter).
"""
```

I ran it, not just read it. `snapshot-a` → `snapshot-b` (both from the original `fixtures/`), same DB:

- Run 1 (snapshot-a): 12 items sent (first-run backfill, expected).
- Run 2 (snapshot-b): **2 items sent** — only the genuinely new "Union responds to port fee inquiry" newsroom item (in both `ops` and `everything`).

Snapshot-b changes exercise all three failure modes the agent names, and all three are handled correctly:
- newsroom rotates `utm_campaign` (`w33`→`w34`) on two unchanged items — suppressed.
- wire regenerates `guid` (`wire-2026-08-14-0031` → `...-r2`) on an edited item, same `link` — suppressed.
- blogroll has no id at all and the title/excerpt is edited, same `permalink` — suppressed.

This is exactly the top-band description: a single strategy stated with its failure modes, verified against all three feeds. Docked one point because "edited item, same link" silently means "never resent" with no size/threshold consideration (e.g. a wire correction that changes the *facts*, like the port-fee inquiry description in snapshot-b, is treated identically to a typo fix) — a real editorial edge case, though the trade-off is at least named in the README.

### Ambiguity handling — 6 / 8

Two of the three canonical forks are named explicitly. Edited-item-as-new-vs-same, in `store.py:52-54`:

```python
"""New" means the link has never been recorded, regardless of source or
raw_id. If a feed ever relinks an item (new URL, same story) it will be
delivered again; that is the right trade-off — a wrong link is easier
to live with than a silently dropped item.
"""
```

First-run backfill, in README.md:19-30:

```
- If you delete `digest.sqlite3`, everything currently in the feeds will
  be re-sent on the next run.
```

The third fork — per-channel vs. global suppression — is decided silently: dedup state is per-link, not per-(link, channel), so an item is looked up/marked exactly once per run regardless of how many channels it fans out to. I verified this is the *correct* choice for the common case (a single item legitimately reaches multiple channels in the same run — `ops` and `energy` both got distinct items from the same `all_items` list in run 1's output) but it is never called out as a decision, and it has a real latent consequence the agent didn't notice: if a channel's keyword filter is broadened later, items that already cleared the global "seen" bar will never reach the newly-matching channel, even on their first match. That's a plausible real-world scenario (someone edits `config.toml`) and it's undocumented.

The agent also caught and documented a bonus ambiguity not in the rubric's list — `--dry-run` still mutates `record_items`' state (README.md:31-32) — which is honest and useful, but doesn't substitute for naming the per-channel fork.

### Failure-mode reasoning — 2 / 8

This is the weak point. `record_items` is called and **committed** before any channel delivery is attempted:

```python
# digest.py:32-58
def run_once(cfg, db, dry_run=False):
    all_items = []
    for feed_cfg in cfg["feeds"]:
        ...
        all_items.extend(store.record_items(db, feed_cfg["name"], items))  # commits here
    ...
    for chan_cfg in cfg["channels"]:
        ...
        channels.send(chan_cfg, body)   # can raise DeliveryError, uncaught
```

`store.record_items` calls `conn.commit()` on every insert (store.py:76) before `run_once` ever reaches the channel loop. `channels.py`'s own docstring says delivery is "best effort: if a channel is down we let the exception propagate and cron will pick us up again on the next tick" — but that contract is now broken, because the *marking* already happened. I reproduced this directly:

```python
cfg["channels"] = [{"name": "ops", "type": "webhook", "url": "http://127.0.0.1:1/nope", "keywords": []}]
sent = digest.run_once(cfg, db)   # raises channels.DeliveryError, uncaught
# -> EXCEPTION: channels.DeliveryError: ops: <urlopen error [Errno 61] Connection refused>
print(store.count_items(db))     # -> 6
```

All 6 fixture items are recorded as "seen" even though delivery never succeeded and the exception propagated out of `run_once` exactly as the original code intended it to (for cron to retry). On the next cron tick, `record_items` will see those links already exist and silently return `[]` for them — they are gone forever, for every channel, not just the one that failed. This is worse than a "partial-failure window": it happens on the very first channel iteration of every run that has *any* channel failure, deterministically. No per-channel marking exists, no ordering discussion exists, and the at-least-once/at-most-once trade-off asserted in the digest.py comment ("each item is delivered at most once") is simply false under failure — it's "at most once, and sometimes zero times, silently."

### Existing-code respect — 4.5 / 6

`feeds.py` and `digest.py` are edited in place, not rewritten; the diff is small and additive (`normalise_link` helper, one field per record touched). `--dry-run` still works and prints the same format. The `items` archive is reused, and the "unconditional insert" problem named in the rubric is directly addressed by making `record_items` return only new rows plus a `UNIQUE` constraint (store.py:30) for defense-in-depth. No scope creep.

Docked for two things: (1) the schema change (`UNIQUE` on `link`) has no migration path — I verified that connecting the new code to an old-schema DB (`CREATE TABLE IF NOT EXISTS`) leaves the constraint absent and any pre-existing duplicate rows in place, silently; and (2) the delivery-ordering bug above is itself a way of "bulldozing" the existing failure contract stated in `channels.py`'s docstring, even though the file wasn't touched.

### Code quality — 2.5 / 4

The code itself is clean, commented, and consistent with the existing style — no dead code, no vestigial columns added. But:

```python
def record_items(conn, source, items):
    ...
    for item in items:
        row = conn.execute("SELECT 1 FROM items WHERE link = ?", (item["link"],)).fetchone()
        if row is not None:
            continue
        conn.execute("INSERT INTO items ...")
        new_items.append(item)
    conn.commit()
```

does a SELECT-then-INSERT per item instead of `INSERT ... ON CONFLICT DO NOTHING RETURNING ...`, which is a minor stylistic nit but not wrong. The bigger quality gap is the missing schema migration for existing databases (see above), which the rubric calls out by name and which I confirmed is simply absent — `CREATE TABLE IF NOT EXISTS` is a no-op against an old table, so the `UNIQUE` constraint (and hence the redundant safety net around the SELECT/INSERT race) silently never applies to any database that predates this change.

There's also a stray indentation glitch in `feeds.py`'s generic-format branch (cosmetic, but sloppy):

```python
    return [
        {
                "title": r["title"],
                "link": normalise_link(r["link"]),
            "summary": r.get("description", ""),
```

### Documentation — 4 / 4

The README gets a real "Deduplication" section (not just a docstring), and it hits all three things the rubric asks for: identity choice and why (with per-feed reasoning), how to reset (delete `digest.sqlite3`), and what happens on first run (everything currently in the feeds gets sent). It also flags the `--dry-run` state-mutation side effect, which is an honest and non-obvious caveat:

```
- If a feed relinks an item (new URL, same story), it will be sent again.
- If you delete `digest.sqlite3`, everything currently in the feeds will
  be re-sent on the next run.
- `--dry-run` records items as seen, so it "uses up" one-run's worth of
  new items.
```

## 3. What it missed

- **Per-channel vs. global suppression** was decided (global) but never named as a decision anywhere in code or docs, despite it being one of the rubric's three canonical forks.
- **Schema migration** for pre-existing databases is entirely absent — `CREATE TABLE IF NOT EXISTS` silently no-ops on an old-schema DB, leaving the `UNIQUE` constraint (and any historical duplicate rows from the old unconditional-insert behaviour) untouched.
- **Delivery-ordering interaction with the existing crash/retry contract** — the agent never seems to have considered that `record_items` commits before `channels.send` runs, which directly contradicts the "best effort, cron retries" comment already sitting in `channels.py`.
- No handling of the case where `--dry-run` is used specifically to *preview without consuming* — the agent noticed and documented the side effect but didn't consider it worth fixing (arguably a legitimate call, but not discussed as a trade-off, just stated as fact).

## 4. Bugs

**Delivery failure causes permanent, silent item loss for the entire batch, not just the failing channel.** Reproduction:

```python
import store, digest
cfg = digest.load_config("config.example.toml")
cfg["channels"] = [{"name": "ops", "type": "webhook", "url": "http://127.0.0.1:1/nope", "keywords": []}]
db = store.connect("test.sqlite3")
digest.run_once(cfg, db)
```

Output:

```
EXCEPTION: channels.DeliveryError: ops: <urlopen error [Errno 61] Connection refused>
items recorded in db: 6
```

All 6 items from the fixture feeds are committed to `items` (and hence excluded from all future runs) even though delivery failed and the exception propagated exactly as `channels.py` intends for cron to retry. Re-running against the same feed data will send 0 items — the batch is gone, for every channel, not just `ops`.

**Cosmetic**: broken indentation in the generic-format list comprehension in `feeds.py` (see Code quality section) — doesn't affect behaviour but would trip up a linter/formatter pass.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 2,
  "existing_code_respect": 4.5,
  "code_quality": 2.5,
  "documentation": 4,
  "total": 28,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid link-based identity, well documented, but marks items sent before delivery succeeds."
}
```
