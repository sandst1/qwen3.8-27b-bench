## 1. Summary

The agent added a `sent_at` column and a link-based `item_key` to `store.py`, then rewrote `run_once` in `digest.py` to skip items whose key has ever been marked sent, with a schema-migration path for existing databases and an updated README section explaining the identity choice. The identity strategy itself is well-reasoned and demonstrably survives all three fixture feeds' quirks (rotating `utm_campaign`, missing `raw_id`, regenerating `guid`), but the suppression state is global-per-item rather than per-channel, which I could reproduce as data loss: a channel that fails delivery after a sibling channel succeeds never receives that item, ever, and a channel added later never receives anything an older channel already delivered. I would not merge as-is — the core dedupe works for the common case, but the cross-channel bug is exactly the kind of thing that will quietly drop notifications in production and it isn't mentioned anywhere in the diff or the README.

## 2. Per-category scoring

### Identity strategy — 8.5 / 10

`store.item_key` (store.py:59-73) keys on `source + link-with-query-and-fragment-stripped`:

```python
def item_key(item):
    """A stable identity for an item that survives edits and re-publishes.

    The three feed formats disagree about what identifies an item:
      * newsroom has a stable entry id, but its URL carries a changing
        `utm_campaign` tag, so the link alone is not stable;
      * blogroll has no id at all — only a permalink;
      * the wire's guid is regenerated every time the item is edited.
    The one field they all agree on is the link, modulo tracking query params,
    so we key on the link with the query string and fragment stripped. The
    source is folded in so two feeds that happen to share a URL don't collide.
    """
    link = item.get("link", "")
    link = link.split("?", 1)[0].split("#", 1)[0]
    return f"{item.get('source', '')}\x1f{link}"
```

I ran this against the fixtures directly. Snapshot-a → snapshot-b changes `utm_campaign=w33` to `w34` on two newsroom items, regenerates the wire's guid (`wire-2026-08-14-0031` → `...-r2`), and edits the blogroll title/excerpt in place. Running snapshot-a then snapshot-b:

```
$ python3 digest.py --config config.example.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.example.toml --db digest.sqlite3   # snapshot-b (config repointed)
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
  ...utm_campaign=w34
sent 2 items
```

Only the genuinely-new item ("Union responds...") and its appearance in the unfiltered channel were sent; the campaign-rotated, guid-regenerated, and text-edited items were correctly treated as already-delivered. The chosen field (link, query-stripped) is the one thing the docstring says all three feeds agree on, and the fixtures back that up. Docked 1.5 points because the strategy silently decides "edited item = same item" (an edit to title/summary with an unchanged link is never re-surfaced) without calling that out as a choice — it rides on the identity mechanism instead of being named as its own fork (see Ambiguity Handling).

### Ambiguity handling — 2.5 / 8

The README documents the identity mechanism, but none of the three canonical forks are named or discussed:

- **Per-channel vs global suppression**: decided silently, and wrongly. `sent_at` is a single column on the shared `items` row, updated by whichever channel first delivers it. There is no per-channel record. I reproduced the failure mode directly:

```
$ python3 digest.py --config config.example.toml --db digest.sqlite3
=== ops ===              # succeeds, marks items sent
Ops digest — 2 item(s)
...
channels.DeliveryError: broken-webhook: <urlopen error [Errno 61] Connection refused>
EXIT: 1
$ python3 digest.py --config config.example.toml --db digest.sqlite3   # retry
sent 0 items
```

`broken-webhook` never received its digest and never will — the items it needed were marked globally sent by `ops` before the run crashed. I also added a fresh no-keyword channel to an already-populated database:

```toml
[[channels]]
name = "late-adopter"
keywords = []
```
```
$ python3 digest.py --config config.example.toml --db digest.sqlite3
sent 0 items
```
A channel that has never sent anything gets nothing, because the suppression state belongs to the item, not to the (item, channel) pair.

- **Edited item as new vs same**: decided silently (same, per the identity strategy) — reasonable, but never stated as a decision anywhere in code comments or README.
- **First-run backfill**: decided silently (send everything present on first run) — not called out either, though this is the least controversial of the three and least likely to surprise anyone.

Per the rubric's own framing, this is "decided silently, and at least one decision is wrong (typically global suppression)" — that's this run exactly.

### Failure-mode reasoning — 3 / 8

`digest.py:52-63`:

```python
    for chan_cfg in cfg["channels"]:
        selected = [i for i in new_items if matches(i, chan_cfg)]
        if not selected:
            continue
        body = render.digest(selected, chan_cfg)
        if dry_run:
            print(f"--- would send to {chan_cfg['name']} ---")
            print(body)
            continue
        channels.send(chan_cfg, body)
        sent += len(selected)
        # Mark delivered right after a successful send so a later failure in
        # this run cannot cause these items to be sent again on the next tick.
        store.mark_sent(db, [store.item_key(i) for i in selected])
```

The comment states an at-most-once intent for the channel that already succeeded, and that half is correct and demonstrated above (`ops` doesn't get a duplicate on retry). But the same mechanism is what causes the channel that failed to lose its delivery permanently — see the `broken-webhook` reproduction above. `channels.send` raising propagates uncaught out of `run_once` (no try/except around it in the per-channel loop), so one channel's `DeliveryError` aborts the whole run and cron sees a non-zero exit — but the items that channel needed are already unreachable, since another channel's earlier success marked them globally sent. This is a partial-failure window that permanently drops delivery for the failing channel, which the rubric places at 2-3; I'm giving it 3 for the fact that ordering (mark-after-send) is at least sound in isolation and there's a comment expressing intent, even though the intent isn't fully realized and the tradeoff is never argued (no discussion of at-least-once vs at-most-once, no discussion of what "channel two fails after channel one succeeds" should do).

### Existing-code respect — 4.5 / 6

The change is additive and scoped: `feeds.py`, `channels.py`, `render.py` are untouched; `digest.py`'s structure (fetch → record → filter per channel → send) is preserved with one new filtering line and one new marking line. The `items` archive table is reused rather than replaced, and the unconditional-insert problem the rubric calls out is explicitly dealt with via `INSERT OR IGNORE` keyed on the new unique index (store.py:82-90), rather than growing unboundedly as it did before. `--dry-run` still works and, correctly, still doesn't mark anything sent (the `continue` in `run_once` happens before `mark_sent`) — verified:

```
$ python3 digest.py --config config.example.toml --db digest.sqlite3 --dry-run   # x2
sent 0 items
sent 0 items
```
(counts identical both times, confirming dry-run truly leaves no trace). `store.migrate()` is a genuine, tested-by-the-agent upgrade path for pre-existing databases (confirmed via the agent's own transcript in `_events.jsonl`, which built an old-schema DB and ran migration against it successfully). Half a point off because the new global-suppression column reuses the one existing `items` table in a way that structurally can't support the per-channel model the failure/ambiguity sections above show is needed — extending the schema now to add a per-(item, channel) delivery table would have been more work but fully compatible with "reuse the archive."

### Code quality — 3.5 / 4

Clean, readable, minimal diff. No dead code was introduced (the one leftover, `count_items`, predates this change and is unused in both the original and the new version — not this agent's doing). SQL is straightforward parameterized `executemany`; the migration is defensive (`PRAGMA table_info`, `ADD COLUMN` guarded by presence checks, `CREATE UNIQUE INDEX IF NOT EXISTS`). Docstrings are accurate and specific rather than generic. Half a point off only because `sent_keys()` loads every ever-delivered key into a Python set every run (`SELECT key FROM items WHERE sent_at IS NOT NULL`) rather than filtering in SQL against the batch just fetched — fine at fixture scale, a latent scaling concern for a long-lived archive, and nothing in the docs flags it as a known tradeoff.

### Documentation — 3 / 4

The README's "De-duplication" section (README.md, new) is more than a docstring pointer — it explains the mechanism (`sent_at`/NULL check), states the identity choice and why, and describes the migration path for existing databases. That clears the "docstring alone caps this at 2" floor comfortably. It does not explain how to reset/force-resend (e.g., "delete the row" or "null out `sent_at` for a key"), and it says nothing about the per-channel-vs-global suppression choice, which is the one a future maintainer most needs surfaced given it's also the one that's broken. Capped short of full marks for that omission.

## 3. What it missed

- **Per-channel delivery state.** The single biggest gap. The fix chosen (a `sent_at` timestamp on the shared item row) can only express "this item has been sent to at least one channel, ever." It cannot express "channel X hasn't gotten this yet." This silently breaks: adding a new channel, temporarily disabling a channel, or one channel's delivery failing while another succeeds.
- **No discussion of what should happen when a channel is added after items already exist in the archive** — reasonable people could argue either "backfill" or "start fresh," but the current behavior (global block) forecloses both options without saying so.
- **No reset/undo story in the README** — an operator who wants to force a re-send (e.g., after fixing `broken-webhook`) has no documented path; they'd have to know to run raw SQL against `sent_at`.
- **No test coverage added.** The agent verified behavior manually in its own session (visible in `_events.jsonl`) but left nothing in the repo — no `tests/`, no fixture-driven regression check — despite `fixtures/snapshot-a` and `snapshot-b` existing for exactly this purpose.

## 4. Bugs

**Bug: a channel that fails once loses its items permanently.**

Reproduction (from a clean run against `fixtures/snapshot-a` with two channels, one a `stdout` "ops" with no filter and one a `webhook` pointed at an unreachable host):

```
$ python3 digest.py --config config.toml --db digest.sqlite3
=== ops ===
Ops digest — 2 item(s)
...
channels.DeliveryError: broken-webhook: <urlopen error [Errno 61] Connection refused>
$ echo $?
1
$ python3 digest.py --config config.toml --db digest.sqlite3
sent 0 items
```

`ops` succeeded and its items were marked `sent_at` (digest.py:60-63). The run then crashed on `broken-webhook`. On retry, `store.sent_keys()` (store.py:93-97) returns those same keys because they were marked sent globally, so they're excluded from `new_items` (digest.py:44-46) for every channel, including the one that never actually got them. `broken-webhook` will never receive that digest unless someone manually edits the database.

**Bug (same root cause): a newly added channel never receives history it hasn't actually seen.**

Reproduction: append a third channel with no keyword filter to an already-populated database and run again — `sent 0 items`, even though this channel has no prior deliveries of its own.

```json
{
  "identity_strategy": 8.5,
  "ambiguity_handling": 2.5,
  "failure_mode_reasoning": 3,
  "existing_code_respect": 4.5,
  "code_quality": 3.5,
  "documentation": 3,
  "total": 25,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid link-based identity, but global (not per-channel) suppression silently drops deliveries on partial failure."
}
```
