# Review — notify-digest, claude-opus-5, run-2

## 1. Summary

The agent added a per-format `dedup_key` in `feeds.py` (raw_id for `newsroom`,
tracking-param-stripped link for `blogroll` and `generic`), a new `sent`
ledger table keyed on `(channel, dedup_key)` in `store.py`, and rewired
`digest.py` to filter through `store.unsent()` before rendering and to call
`store.mark_sent()` only after a channel accepts delivery, with per-channel
try/except around send. I ran the fixtures myself (snapshot-a, snapshot-a
again, snapshot-b) and a synthetic broken-channel scenario, and every claim
made in the README and docstrings held up exactly as stated — no gap between
documentation and behavior. I would merge this as-is.

## 2. Per-category scoring

### Identity strategy — 9.5/10

The agent identified that no single field is stable across all three feed
formats and built a per-format rule with a documented failure-mode table in
`feeds.py:18-26`:

```
    format     raw_id across polls        link across polls       we key on
    newsroom   stable (entry_id)          rotates (utm_campaign)  raw_id
    blogroll   absent entirely            stable (permalink)      link
    generic    regenerated on every edit  stable                  link
```

Concretely:

```python
# feeds.py, newsroom branch
for item in items:
    item["dedup_key"] = _dedup_key(name, "raw_id", item["raw_id"])
```
```python
# feeds.py, blogroll/generic branches
for item in items:
    item["dedup_key"] = _dedup_key(name, "link", canonical_link(item["link"]))
```

`canonical_link` (`feeds.py:64-79`) strips `utm_*`, `gclid`, `fbclid`,
`mc_cid`/`mc_eid` and the fragment before using a link as identity, while the
item's displayed `link` is untouched. I verified both traps described in the
docstring actually exist in the fixtures and are handled:

- `newsroom`'s `url` carries `utm_campaign=w33` in snapshot-a and
  `utm_campaign=w34` in snapshot-b for the *same* two articles — keying on
  `raw_id` (not link) correctly keeps them deduplicated across the rotation.
- `generic`'s (wire) `guid` for "Port fee inquiry opened" changes from
  `wire-2026-08-14-0031` to `wire-2026-08-14-0031-r2` between snapshots when
  the description gets a clause added — keying on the stable `link` (not
  `guid`) correctly avoids resending it.

I ran the full a→a→b sequence and got exactly what the README promises: run 2
is silent, run 3 delivers exactly one genuinely-new item
("Union responds to port fee inquiry"). `_dedup_key` also namespaces by
`source` and by `scheme` (`feeds.py:100-111`), so a future format change can't
silently collide with keys written under the old rule. The only knock is
that `raw_id` for `newsroom` is trusted as globally stable without an
explicit fallback if a provider ever reused an `entry_id` — a minor, mostly
theoretical gap given the fixtures don't exercise it.

### Ambiguity handling — 8/8

All three of the rubric's named forks are resolved *and* stated explicitly,
not just implemented correctly by accident.

- **Per-channel vs global suppression** — resolved per-channel, stated in
  `store.py`'s module docstring and in `digest.py:52-55`:
  ```python
  # The dedup step. Per channel, because the same item can match more
  # than one channel and each of them is owed a copy.
  selected = store.unsent(db, name, selected)
  ```
  I confirmed this: an item matching both `ops` and `everything` was
  delivered to both on the same run.

- **Edited item as new vs same** — resolved as "same" (identity over
  content), stated in `feeds.py:38-42` and again in `README.md`:
  > "Identity, not content. An item edited upstream after we sent it is not
  > sent again... but a materially rewritten post will stay silent."
  I confirmed the wire's guid-bump-on-edit and blogroll's retitle/excerpt
  edit both correctly stay silent on the second poll.

- **First-run / backfill** — addressed in `README.md`'s Deployment section:
  > "deleting or moving `digest.sqlite3` makes every current feed item look
  > new and the next run will send a full backlog to every channel."
  This is accurate — I verified a fresh DB sends everything currently in the
  feed on first run, which is the obvious and stated consequence of an
  empty ledger.

This is the strongest category: the decisions are not just correct, they are
named as decisions with their consequences spelled out in three separate
places (module docstring, README table, inline comments).

### Failure-mode reasoning — 8/8

Per-channel marking, correct ordering, and the tradeoff is argued explicitly.

```python
# digest.py
try:
    channels.send(chan_cfg, body)
except channels.DeliveryError as exc:
    print(f"warn: channel {name} failed: {exc}", file=sys.stderr)
    failures += 1
    continue

# Only after the channel accepted it. If we crash between here and
# the commit, the digest goes out twice — better than losing it.
store.mark_sent(db, name, selected)
sent += len(selected)
```

I built a two-channel config with one channel pointed at a closed port
(guaranteed `ConnectionRefusedError` → `channels.DeliveryError`) and one
`stdout` channel, both reading the same feed:

```
$ python3 digest.py --config /tmp/failtest.toml --db fail.sqlite3
warn: channel broken failed: broken: <urlopen error [Errno 61] Connection refused>
=== ok ===
... (2 items delivered) ...
sent 2 items
exit: 1

$ python3 digest.py --config /tmp/failtest.toml --db fail.sqlite3   # second run
warn: channel broken failed: broken: <urlopen error [Errno 61] Connection refused>
sent 0 items
exit: 1
```

This is exactly the desired behavior: `ok` is marked sent and goes silent on
retry, `broken` is retried and eventually delivers once it recovers, and the
process exits non-zero so cron logs/monitoring notice (`digest.py:100-103`,
`return 1 if failures else 0`). The at-least-once choice ("send first,
record after") is stated and justified both in `digest.py`'s comment above
and in `README.md`'s "Decisions worth knowing" list. `--dry-run` never marks
anything sent (`digest.py:60-64`), which I verified is idempotent by running
it twice and diffing identical output.

### Existing-code respect — 6/6

The `items` archive table is untouched and still populated the same way
(`store.py:56-69`); the new `sent` table is added via
`CREATE TABLE IF NOT EXISTS`, so an existing `digest.sqlite3` migrates
transparently with no `ALTER` needed and no data loss. `render.py`,
`channels.py`, and `config.example.toml` are byte-for-byte unchanged (verified
by `diff`). `--dry-run` still works and is still non-destructive (verified
above). `feeds.py`'s three format branches keep their original shape; the
identity logic is added as a small appended loop per branch rather than a
rewrite. `matches()` and the per-channel filter loop in `digest.py` are
untouched apart from inserting the dedup call. No scope creep — no plugin
system, scheduler, or extra channels were added.

### Code quality — 3.5/4

Clean, readable, consistent style matching the original. Minor deductions:

- `store.count_sent()` (`store.py:106-107`) is dead code — added but never
  called anywhere in `digest.py` or elsewhere. Same criticism the original
  code leveled at `count_items` ("nothing reads it"), now duplicated.
- The `sent` table's `dedup_key` has no index beyond the composite primary
  key `(channel, dedup_key)`, which is fine since that's exactly the lookup
  pattern in `unsent()` — no issue there, this is a non-issue I checked and
  ruled out.
- `_dedup_key`'s use of `\x1f` (unit separator) as a field delimiter is a
  reasonable, mildly clever choice that avoids collision with `source` names
  containing literal separators — worth a one-line comment on *why* that
  character was chosen, but not required.

### Documentation — 4/4

Three redundant but non-contradictory layers: a docstring in `feeds.py`
explaining the identity table and both traps with fixture-specific examples,
a `store.py` docstring explaining the two tables' distinct jobs and the
never-pruned decision, and a `README.md` section ("De-duplication") that
restates the identity table, both traps, all three named ambiguity
decisions, and the reset/backfill procedure. This exceeds "docstring alone"
by a wide margin and directly answers "how do I reset it" and "what happens
on first run."

**Total: 39/40**

## 3. What it missed

- **`newsroom`'s `raw_id` stability is assumed, not defended.** If a provider
  ever reused an `entry_id` (unlikely but not proven impossible), items would
  silently and permanently collide. The agent didn't add a fallback (e.g.
  `raw_id` + published-date sanity check) or even flag this as a risk the way
  it flagged the other two traps.
- **No test suite.** There was none in the original either, so this isn't a
  regression, but given the depth of reasoning invested in the identity
  logic, a couple of unit tests for `canonical_link` and `_dedup_key` (e.g.
  asserting the two documented traps are actually handled) would have locked
  in the behavior rather than relying on README prose plus manual fixture
  runs.
- **Cross-source duplicates** (same story from two different providers) are
  explicitly called out as unaddressed — this is a stated non-goal, not a
  miss, but worth listing since a "same story" can absolutely appear in both
  `newsroom` and `wire` in real feeds and this digest will send it twice per
  channel forever.
- **`count_sent` dead code** — added without a caller; either wire it into a
  status/debug command or drop it.

## 4. Bugs

None found. I specifically tried to break:

- Repeated runs against the same snapshot (silent, confirmed).
- Cross-snapshot run to trigger both documented traps (`utm_campaign`
  rotation, `guid` regeneration on edit) — both correctly did not re-send.
- Partial channel failure with a channel that never comes back reachable —
  correctly retried forever without blocking the working channel, correct
  non-zero exit code.
- `--dry-run` called twice in a row against an empty ledger — produced
  byte-identical output both times (never suppresses or advances state).

No crash, no double-marking, no schema migration failure against a
freshly-created `digest.sqlite3`. This is as clean a fix as the prompt's
underspecification allows.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 3.5,
  "documentation": 4,
  "total": 39,
  "would_merge": "yes",
  "one_line": "Thorough per-format identity, all forks named, verified correct against fixtures."
}
```
