# Review — notify-digest dedup fix

## 1. Summary

The agent added a per-channel delivery ledger (`deliveries(channel, key)` in `store.py`) and computed a per-feed "stable key" in `feeds.py` — `entry_id` for newsroom, `link` for blogroll and wire — rather than trusting the existing `raw_id` field uniformly. I verified empirically (running snapshot-a then snapshot-b through the unmodified fixtures) that this correctly suppresses re-sends across ticks, survives the newsroom `utm_campaign` rotation, the blogroll's missing id, and the wire feed's guid regeneration on edit, and that a crash mid-run leaves already-delivered channels marked while a failed channel is retried without re-notifying the channels that succeeded. It is undocumented on two real decisions — what happens on the very first run against a feed/database with history, and how to undo the ledger — and the crash/retry tradeoff it implements correctly is never argued in words. I would merge this with a follow-up ticket for the first-run flood, not as-is without comment.

## 2. Per-category scoring

### Identity strategy — 9.5 / 10

The agent did not pick one rule; it picked three, one per feed shape, in `feeds.py`:

```python
# newsroom (feeds.py:47-62)
"raw_id": str(r["entry_id"]),
# entry_id is stable across edits; the url is not (the provider
# re-appends utm params), so entry_id is the dedup anchor.
"key": str(r["entry_id"]),
```
```python
# blogroll (feeds.py:64-78)
# No provider id here at all, and titles/excerpts get edited in place.
# The permalink is the only thing that stays put, so dedup on the link.
...
"raw_id": None,
"key": r["permalink"],
```
```python
# generic/wire (feeds.py:80-95)
# "generic": has a guid, but the provider regenerates it whenever an
# item is edited (typos, added tags, retitles) — e.g. "wire-...-0031"
# becomes "wire-...-0031-r2". That makes guid useless for dedup; the link
# is the stable anchor, so key on it despite raw_id being populated.
...
"raw_id": r.get("guid"),
"key": r["link"],
```

I ran this against both fixture snapshots to confirm it actually holds up (`/tmp/nd_test`, unmodified agent code, config pointed at `snapshot-a` then `snapshot-b`):

- Run 1 (`snapshot-a`): 12 items sent across channels (first run, nothing in the ledger yet — see §3 and §4).
- Run 2 (`snapshot-a` again, identical tick): `sent 0 items` — no accidental re-send on an unchanged feed.
- Run 3 (`snapshot-b`): only the genuinely new newsroom entry (`entry_id 84130`, "Union responds...") was sent. The newsroom items whose `url` picked up `utm_campaign=w34` were correctly *not* re-sent (key is `entry_id`, unaffected). The wire item whose `guid` changed from `wire-2026-08-14-0031` to `...-r2` was correctly *not* re-sent (key is `link`, unaffected). The blogroll post retitled "(updated)" was correctly *not* re-sent (key is `permalink`).

That is exactly the top-band description — "per-feed identity that survives rotating `utm_campaign`, a missing `raw_id`, and a regenerated `guid`" — and it's stated with failure modes in the README table (quoted under Documentation below), not just in code comments. Docking half a point because the stated rule ("key on `link` because `raw_id` rotates") is never qualified for the case where the *link itself* changes on edit (not exercised by the fixtures, but also not flagged as an open question) — see §3.

### Ambiguity handling — 6 / 8

Three forks exist in this problem. Two are named and resolved in writing; one is never raised.

**Per-channel vs. global suppression** — named and resolved, in both the code and the README:

```python
# digest.py:46-54
seen = store.delivered_keys(db, chan_cfg["name"])
# Send only items this channel has not received on a previous tick.
# `seen` is read from the persisted ledger, so delivering the same
# item to another channel in this same run is not suppressed.
selected = [
    i for i in all_items
    if matches(i, chan_cfg) and i.get("key") not in seen
]
```
> "Delivery is per-channel: the same story can land in several channels in one run, and each gets it once." (README.md:35-36)

I confirmed this is real, not aspirational: in Run 1, "Port fee inquiry opened" [wire] appears in both the `ops` digest and the `everything` digest in the same invocation.

**Edited item as new vs. same** — named and resolved (decided: same item, suppress):

> "Because of the wire feed, 'just dedup on `raw_id`' is wrong: an edited wire item arrives with a brand-new guid and would be resent every time it was tweaked." (README.md:47-49)

**First-run backfill** — not named anywhere. I grepped the README, every docstring, every comment, and the agent's own chat log (`_agent.log`) for "first run", "backfill", "backlog" — nothing. And it's not a theoretical gap: running the agent's code against a *pre-existing* database that only has the old `items` schema (simulating a box that's already been running this cron job for months) produces the same full 12-item flood as a truly fresh database, because `deliveries` starts empty regardless of what's in `items`. That's a real "people keep getting the same items" regression risk for exactly the population this fix is meant to help, and it's undiscussed. Two of three forks named and correctly resolved, the third entirely unrecognized — solidly the "4-6" band, and I'm putting it at the top of that band because the two that are named are also independently verified correct, not just asserted.

### Failure-mode reasoning — 5 / 8

The mechanism is right: `store.mark_delivered` is only called after `channels.send` succeeds, per channel, and commits immediately (`store.py:78-88`), so a crash doesn't lose or duplicate a channel's delivery state for channels already processed. I reproduced this directly: with two channels (`ops` = stdout, `broken` = webhook to a closed port), the first run sends to `ops` and then raises `DeliveryError` on `broken` with exit code 1. A second run (after fixing `broken`) sends **only** to `broken`'s two items — `ops` is not re-notified, even though the process crashed mid-run on the first attempt. That's a genuine at-least-once-per-channel guarantee, correctly ordered.

```python
# digest.py:57-64
body = render.digest(selected, chan_cfg)
if dry_run:
    print(f"--- would send to {chan_cfg['name']} ---")
    print(body)
    continue
channels.send(chan_cfg, body)
store.mark_delivered(db, chan_cfg["name"], selected)
sent += len(selected)
```

What's missing is the argument. Nowhere — not the README, not a comment near this block, not `channels.py`'s existing note about best-effort delivery — is the at-least-once choice named as a choice, or the tradeoff (a crash between `send` and `mark_delivered` can duplicate a channel's items on retry; the system never *drops* an item, but it can double-send one) spelled out. This is exactly the rubric's "4-6: per-channel marking, sane ordering, no discussion" band, not "7-8" which requires the tradeoff to be argued.

### Existing-code respect — 6 / 6

`channels.py`, `render.py`, and `config.example.toml` are byte-for-byte untouched (confirmed with `diff`, zero output). `feeds.py` keeps every existing field and only adds `key`; no reformatting of its logic. Crucially, the agent did not try to repurpose the existing `items` archive — which the rubric flags as a trap, since `store.record_items` inserts unconditionally every tick with no uniqueness constraint — for dedup. It left that table and its docstring's stated purpose ("an archive... we do not read it to decide what to send", `store.py:5-7`) alone and built a separate `deliveries` table for the ledger. `--dry-run` still works and still reflects what *would* be suppressed, without writing to the ledger (the `continue` on `digest.py:60` is before `mark_delivered`), which I confirmed by running `--dry-run` repeatedly and seeing identical output each time. No scope creep — no new CLI flags, no new dependencies, no speculative abstraction.

### Code quality — 3.5 / 4

The new SQL is simple and correct: `deliveries` has a composite primary key `(channel, key)` and is written with `INSERT OR IGNORE`, so the per-channel-per-item uniqueness constraint lives in the schema, not in application logic. Migration for a database created by the *old* code is handled for free via `CREATE TABLE IF NOT EXISTS` — I verified this by building a database with the pristine schema (`items` only) and running the new `digest.py` against it; it adds `deliveries` with no error and no manual migration step, and this is stated outright:

> "`deliveries` is created with IF NOT EXISTS, so a database from before this table existed just gets it on the next run — no migration step." (store.py:12-13)

The one real defect: the edited module docstring in `feeds.py` has broken indentation — lines 6-15 are indented four spaces deeper than lines 1-4, and the closing `"""` sits at that same wrong indent instead of flush left:

```
feeds.py:1-15 (cat -e)
"""Feed loading.$
$
Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones$
(used in development and in the fixtures/ directory) are read from disk.$
$
    The three providers we currently pull from do not agree on much, so this$
    module normalises them into a common item shape:$
...
    """$
```
Harmless at runtime (it's inside the triple-quoted string either way, confirmed via `feeds.__doc__`), but it's the kind of sloppy diff a reviewer would bounce back for a second pass.

### Documentation — 2.5 / 4

The README gets a real, non-trivial new section, not just a docstring:

```markdown
## Deduplication

Each cron tick only sends items a channel has not received before. We keep a
`deliveries(channel, key)` ledger and skip anything already in it for that
channel. Delivery is per-channel...

| Feed | Stable key | Why not something else |
| --- | --- | --- |
| newsroom | `entry_id` | the `url` changes on every edit (utm params) |
| blogroll | `link` (permalink) | no provider id at all; titles/excerpts are edited in place |
| wire | `link` | the `guid` (`raw_id`) is regenerated on every edit |
```
(README.md:31-50)

That's a solid explanation of *the dedupe behaviour* — the first of the three things the rubric asks for. The other two are both absent: there is no sentence anywhere about *how to reset it* (delete `digest.sqlite3`? drop the `deliveries` table? there's no flag and no instruction either way), and nothing about *what happens on first run* against a feed that already has a backlog — which, per §2's ambiguity-handling finding and the migration test in §4, is a real flood, not a hypothetical. Two of three required topics missing caps this below the midpoint.

## 3. What it missed

- **First-run / pre-existing-backlog flood.** Neither raised nor resolved. Deploying this fix to the actual cron box described in the prompt ("we run this every 15 minutes," implying it's already been running and presumably already has a populated `items` archive and a feed full of items people have already seen many times) means the very first tick after the fix ships re-sends everything currently in the feed, because `deliveries` starts empty independent of `items`. Confirmed with a migration test (§4). A one-line decision — backfill `deliveries` from the existing `items` archive on first creation, or just say in the README "the first run after deploying this will re-send everything currently live in the feeds, after which it settles down" — was available and not taken, silently or otherwise.
- **How to reset the ledger.** No flag, no documented manual step (e.g., `DELETE FROM deliveries WHERE channel = ...` or drop the table) for the ops case of "we want channel X to get everything again."
- **Crash/retry tradeoff, argued.** The implementation is at-least-once-per-channel and correct (§2.3), but the choice is never named as a choice anywhere a future maintainer would read it.
- **Link-changes-on-edit case for wire/blogroll.** The identity choice for these two feeds is "key on link because the id rotates." If a future edit to a wire or blogroll item changes its link (e.g., URL slug corrected), that item would be treated as brand new and resent — symmetric to the exact bug this fix was written to solve, just moved to the link rather than the id. Not fixture-exercised, not flagged as a residual risk.
- **Docstring formatting regression** in `feeds.py` (§2.5) — not a functional bug, but it's the sort of thing that signals the diff wasn't re-read before finishing.

## 4. Bugs

No functional bugs found. The one reproducible, concrete gap is the first-run/migration flood described above, which I'd class as a missed decision rather than a bug — the code behaves consistently and documented, just not usefully for an already-running deployment:

```sh
# Reproduction: simulate a box that has been running the pristine job for a while
# (database has the old `items` table and history, but — necessarily — no
# `deliveries` table, since that's new).
python3 -c "
import sqlite3
conn = sqlite3.connect('old.sqlite3')
conn.executescript('''CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, raw_id TEXT,
    title TEXT NOT NULL, link TEXT NOT NULL, summary TEXT, published TEXT,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')));''')
"
python3 digest.py --config config.toml --db old.sqlite3
# -> sent 12 items, identical to a brand-new database with zero history.
# Every item currently live in the feed gets (re-)delivered to every matching
# channel on the very first run of the fixed code, with no warning.
```

This is not catastrophic (it self-heals after one tick), but for a notification system whose entire bug report is "people keep getting the same items over and over," shipping a fix whose first observable effect in production is "everyone gets everything currently in the feed one more time" deserved at least a sentence of acknowledgment.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 5,
  "existing_code_respect": 6,
  "code_quality": 3.5,
  "documentation": 2.5,
  "total": 32.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Correct per-feed dedup, verified; first-run flood and retry tradeoff left undocumented."
}
```
