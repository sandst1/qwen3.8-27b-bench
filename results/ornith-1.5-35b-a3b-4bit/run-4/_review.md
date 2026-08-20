# Review — notify-digest, run-4

## 1. Summary

The agent added a `delivered` ledger table keyed on `(channel, source, canonicalized link)`, checked it before each send and wrote to it right after, leaving `feeds.py`, `render.py`, and `channels.py` untouched. I ran it against both fixture snapshots: a second run against `snapshot-a` sends 0 items, and `snapshot-b` correctly sends only the genuinely new article while suppressing the utm-rotated re-fetch of two already-seen articles. I would merge this with fixes — the mechanism works and is minimally invasive, but the README wasn't actually updated to explain reset/first-run behavior, and two of the three ambiguity forks (edited-item resend, first-run backfill) were resolved silently rather than called out.

## 2. Per-category scoring

### Identity strategy — 8/10

`store.py`:

```python
def _dedup_key(item):
    """Identity of an item for the dedup ledger: source + link without query."""
    parts = urlsplit(item.get("link") or "")
    canonical = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return (item.get("source", ""), canonical)
```

and the docstring justifying it:

> `raw_id` is sometimes absent (blogroll) and sometimes rewritten when an item is edited (generic), and feed providers tack tracking params (UTM tags, campaign ids) onto the link that change per fetch while the article underneath does not.

I verified this against both fixtures directly:

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a again
sent 0 items
$ sed -i '' 's/snapshot-a/snapshot-b/' config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-b
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
  https://newsroom.example/2026/08/port-fees-union?utm_source=feed&utm_campaign=w34
sent 2 items
```

It correctly ignores the `utm_campaign=w33→w34` rotation on the two already-seen newsroom items, doesn't care that blogroll has no `raw_id`, and doesn't care that wire's `guid` gets a `-r2` suffix on edit. It also correctly identifies the one item whose link path actually changed (`port-fees-union`) as new. This holds up against all three feeds and the choice is stated in the docstring — but the docstring only argues the failure modes of the *rejected* alternative (`raw_id`), not the failure mode of the chosen one: an item that gets a substantive rewrite while keeping its URL (the wire and blogroll edits in `snapshot-b`) will never re-notify. That's a real, foreseeable consequence of this identity choice and it isn't named anywhere. That gap is why this isn't a 9–10.

### Ambiguity handling — 5/8

Per-channel vs. global suppression is named and resolved explicitly, in both the docstring and the schema:

```sql
CREATE TABLE IF NOT EXISTS delivered (
    channel   TEXT NOT NULL,
    source    TEXT NOT NULL,
    link      TEXT NOT NULL,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, link)
);
```

Confirmed by inspecting the ledger after the runs above — `wire.example/i/0031` is recorded separately under `ops` and `everything`.

The other two forks are not named:

- **Edited item as new vs. same**: silently decided as "same" (see identity discussion above), and it's the correct call for this benchmark's fixtures, but nothing in the code or docs says "an edit to an already-delivered item will not be re-sent."
- **First-run backfill**: on a fresh database, `run_once` sends the *entire* current feed contents immediately (12 items in my first test run) rather than seeding the ledger without notifying. This is a real decision — a fresh deploy on a full production feed would flood every channel on the very first cron tick — and it isn't discussed or mentioned anywhere.

One of three forks resolved and stated; the other two decided silently (and happen to be reasonable). That's squarely mid-band.

### Failure-mode reasoning — 5/8

`digest.py:46-58`:

```python
for chan_cfg in cfg["channels"]:
    selected = [i for i in all_items if matches(i, chan_cfg)]
    unseen_items = store.unseen(db, chan_cfg["name"], selected)
    if not unseen_items:
        continue
    body = render.digest(unseen_items, chan_cfg)
    if dry_run:
        print(f"--- would send to {chan_cfg['name']} ---")
        print(body)
        continue
    channels.send(chan_cfg, body)
    store.record_delivered(db, chan_cfg["name"], unseen_items)
    sent += len(unseen_items)
```

Marking is per-channel and ordered correctly for an at-least-once guarantee: the ledger write happens *after* `channels.send` returns, and `record_delivered` commits immediately (`store.py`, end of file). If channel 2's `channels.send` raises (per `channels.py`'s documented "let the exception propagate" behavior), channel 1 is already committed as delivered and won't be resent; channel 2's items are simply retried whole on the next cron tick. That's sane and matches the existing "delivery is best-effort" comment in `channels.py`. But this ordering choice, and the at-least-once tradeoff it implies (a channel can never get skipped items, but could theoretically double-send if the process dies between a successful webhook POST and the `commit()` a few lines later), is never argued anywhere — no comment in `digest.py`, no README note. Correct mechanism, no discussion, which is exactly the 4-6 band.

### Existing-code respect — 6/6

`feeds.py`, `render.py`, and `channels.py` are untouched (confirmed by `diff -rq` against the original — only `digest.py`, `store.py`, and one README table row changed). The existing `items` archive and its unconditional insert are left alone and the new `delivered` table is added alongside it with `CREATE TABLE IF NOT EXISTS`, so an existing database on a production box picks up the new table without a manual migration step. `--dry-run` still short-circuits before `channels.send`/`record_delivered` (`digest.py:52-55`), confirmed working:

```
$ python3 digest.py --dry-run ...   # (checked in scratch copy, no ledger writes occur)
```

Minimal, surgical diff. Full marks.

### Code quality — 4/4

The new code is short, readable, and has no dead code or vestigial columns. The `IN` clause construction in `unseen()`:

```python
placeholders = ",".join("(" + ",".join("?" * 2) + ")" for _ in keys)
params = [channel]
params.extend(key for k in keys for key in k)
seen = conn.execute(
    "SELECT source, link FROM delivered "
    "WHERE channel = ? AND (source, link) IN (" + placeholders + ")",
    params,
).fetchall()
```

is a slightly unusual way to do a bulk membership test but it's parameterized (no SQL injection risk) and I confirmed by running it that SQLite's row-value `IN` syntax works as expected here. `ON CONFLICT ... DO NOTHING` in `record_delivered` is the right idempotent-insert idiom.

### Documentation — 2/4

The only prose documentation of the whole ledger mechanism is the `store.py` module docstring (thorough, and I've quoted it above — it does explain what the ledger is and why link-over-raw_id was chosen). The README's contribution is a single table-cell edit:

```diff
-| `store.py` | SQLite archive of everything seen |
+| `store.py` | SQLite: `items` archive + `delivered` ledger that stops re-sending |
```

Nothing in the README says how to reset the ledger (delete `digest.sqlite3`? clear one table? no `--reset` flag was added), and nothing anywhere states the first-run flood behavior. Per the rubric, a docstring alone caps this category at 2, and that's what happened here — the actual prose documentation is a docstring, not a README explanation.

## 3. What it missed

- **First-run backfill** is unaddressed both in code and prose: a fresh `digest.sqlite3` against a live, populated feed will fire every existing item to every matching channel on the very first cron tick. No `--seed`/`--mark-only` flag, no README caveat.
- **Edited-item semantics** are a real, silent design choice: because identity is link-only, an already-delivered item that gets a substantive edit (as both the wire and blogroll fixtures demonstrate in `snapshot-b`) will never trigger a second notification. This is arguably the right call, but it's never stated as a decision, only implied by the identity discussion.
- **No reset mechanism**: there's no documented way to force a resend (e.g., truncate `delivered`, or a `--reset-channel` flag) despite the README claiming to explain "how to reset it" style topics elsewhere.
- **No test coverage** was added; the fix was verified only by the reviewer running it manually against the fixtures, not by anything checked into the repo.

## 4. Bugs

None found. Both fixture snapshots run cleanly through `--dry-run` and live modes; the ledger dedupes correctly across repeated runs and across the utm-rotation case, per-channel suppression works as intended, and `--dry-run` doesn't write to the ledger. I did not have a way to exercise the webhook failure path (no fixture provides a failing webhook target), so the partial-failure ordering was checked by code inspection rather than reproduced.

```json
{
  "identity_strategy": 8,
  "ambiguity_handling": 5,
  "failure_mode_reasoning": 5,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 2,
  "total": 30,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid per-channel link-based dedupe; verified working, but two forks and README undocumented."
}
```
