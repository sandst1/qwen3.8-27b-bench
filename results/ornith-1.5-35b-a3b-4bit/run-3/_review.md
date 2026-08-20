# Review — notify-digest dedup fix (ornith-1.5-4bit, run-3)

## 1. Summary

The agent added a link-based dedup layer: `store.normalize_link()` strips
query strings and fragments, `store.seen_links()` reads every link ever
archived, and `digest.run_once` filters `all_items` against that set before
building each channel's digest — a genuinely well-reasoned identity choice
that correctly survives `utm_campaign` rotation, a missing `raw_id`, and a
regenerated `guid` across all three fixture feeds. But the implementation
reuses the existing unconditional `record_items()` insert as the dedup
source of truth without addressing when it fires, which produces two
provable regressions: `--dry-run` permanently marks items as sent even
though nothing was delivered, and suppression is global across channels
rather than per-channel, so a new or newly-matching channel will silently
never receive an item another channel already got. I would not merge this
as-is — the identity logic is worth keeping, but the marking/dry-run/global-
suppression bugs need to be fixed first, and the README needs to say any of
this happened at all.

## 2. Per-category scoring

### Identity strategy — 9/10

`store.py`:

```python
def normalize_link(link):
    """Stable identity key for an item, derived from its link.

    The link is the one field every feed format shares and the least likely to
    change. raw_id is unusable as a key: blogroll has none, and the generic
    provider regenerates its guid whenever an item is edited. Stripping the
    query string keeps tracking params (utm_campaign, and friends) from making
    the same item look new.
    """
    return link.split("?", 1)[0].split("#", 1)[0]
```

This is exactly the reasoning the rubric wants: the choice is named, and it
addresses all three fixture hazards, which I verified directly:

- `wire`: guid changes between snapshots (`wire-2026-08-14-0031` →
  `wire-2026-08-14-0031-r2`) but the link doesn't — link-keying survives it.
- `newsroom`: `utm_campaign` rotates `w33` → `w34` on the same article — the
  query-string strip survives it.
- `blogroll`: has no `raw_id` at all (`feeds.py` confirms: `"raw_id": None`)
  — link is the only usable key, and it's what's used.

Running snapshot-a then snapshot-b through the same db, only the one
genuinely new newsroom item ("Union responds to port fee inquiry") comes out
as fresh; the three edited-but-same-link items are correctly treated as
already seen. Docked half a point because the normalization is naive
(no scheme/trailing-slash/case handling) and the docstring doesn't own that
limitation — but for the fixtures given, it holds up completely.

### Ambiguity handling — 3/8

None of the three canonical forks are named as decisions anywhere in the
code, comments, or README. One of them — per-channel vs. global suppression
— is picked silently and is wrong. Reproduction:

```
$ cat >> config.toml <<'EOF'
[[channels]]
name = "compliance"
type = "stdout"
keywords = ["levy"]
EOF
$ python3 digest.py --config config.toml --db digest.sqlite3
sent 0 items
```

Several already-archived items contain "levy" ("Working through the levy
numbers", "back the levy", "the levy is unlawful") but the new `compliance`
channel — which has never received anything — gets nothing, because
`digest.py`'s dedup is global:

```python
fresh = []
for item in all_items:
    key = store.normalize_link(item["link"])
    if key in already_sent or key in {store.normalize_link(j["link"]) for j in fresh}:
        continue
    fresh.append(item)
...
for chan_cfg in cfg["channels"]:
    selected = [i for i in fresh if matches(i, chan_cfg)]
```

`already_sent` (`store.seen_links(db)`) is computed once from the whole
archive, with no notion of which channel actually got which item. Add a
channel, retitle a channel's keywords, or fix a broken channel and back-fill
— all of these silently get nothing for items already in the archive, forever.

Edited-item-as-new-vs-same is decided silently as "same" (link-keyed) —
reasonable, and it happens to be what the fixtures want, but it is never
stated as a decision anyone made; the docstring talks about *why* raw_id is a
bad key, not about what happens to edited items going forward.

First-run backfill is not discussed at all — on an empty db, the entire
fixture set gets sent as one large digest, unremarked in code or README.

### Failure-mode reasoning — 2/8

`digest.py`'s `run_once` still calls `store.record_items()` — which commits
immediately — before any channel is attempted, and that same table is the
dedup source:

```python
def run_once(cfg, db, dry_run=False):
    already_sent = store.seen_links(db)
    all_items = []
    for feed_cfg in cfg["feeds"]:
        ...
        store.record_items(db, feed_cfg["name"], items)   # commits here
        all_items.extend(items)
    ...
    for chan_cfg in cfg["channels"]:
        ...
        channels.send(chan_cfg, body)                      # may raise/fail
```

Reproduction — a webhook channel that fails outright:

```
$ python3 digest.py --config config.toml --db digest.sqlite3
...
channels.DeliveryError: broken: <urlopen error [Errno 61] Connection refused>
$ sqlite3 digest.sqlite3 "select link from items"
https://newsroom.example/2026/08/port-fees?...
https://newsroom.example/2026/08/offshore-tender?...
```

The items are archived — and therefore "seen" for all future runs — even
though delivery crashed before anything was sent. There is no per-channel
marking at all (there's only one marking point, at fetch time, before any
channel is even considered), no discussion of at-least-once vs at-most-once,
and the actual behavior that falls out is "lose the item forever on any
crash between fetch and send," which is arguably the worst of the three
named options.

A second, more damaging instance of the same defect — `--dry-run` is now
load-bearing:

```
$ python3 digest.py --config config.toml --db digest.sqlite3 --dry-run
--- would send to ops ---
... (3 items) ...
$ python3 digest.py --config config.toml --db digest.sqlite3
sent 0 items
```

`--dry-run` is supposed to be a no-op preview. Because `record_items()` runs
unconditionally regardless of `dry_run`, and the new dedup reads that same
table, a single dry run permanently poisons delivery — those items will
never actually be sent by a real run. This is silent data loss triggered by
the tool's own preview flag.

### Existing-code respect — 2/6

The rubric calls this out directly: "Reusing the `items` archive is fine *if*
the unconditional insert is dealt with." The agent reused the archive as the
dedup source and left the unconditional insert completely untouched
(`digest.py` still calls `store.record_items(db, feed_cfg["name"], items)`
for every fetched item, every run, regardless of whether it's new). This is
precisely the failure mode the rubric warns about, and it's what causes both
the `--dry-run` breakage and the crash-loses-item behavior above. `feeds.py`
and `channels.py` are untouched, `--dry-run`'s CLI surface is untouched, but
its *effective behavior* is now broken, which the rubric explicitly weighs
here.

### Code quality — 2.5/4

Readable, minimal diff, sensible SQL, no schema change needed so no
migration question arises. Two dings:

- `digest.py`'s within-run dedup rebuilds a set from scratch on every item:

  ```python
  if key in already_sent or key in {store.normalize_link(j["link"]) for j in fresh}:
  ```

  O(n²) and does redundant `normalize_link` recomputation; should be an
  incrementally-updated set.
- The unconditional-insert issue (above) leaves the `items` table growing
  with duplicate rows for the same link on every 15-minute tick forever —
  confirmed directly: after 3 runs against snapshot-a/snapshot-a/snapshot-b,
  `https://blog.example/port-fee-arithmetic` appears **4 times** in `items`
  despite being delivered exactly once.

### Documentation — 1.5/4

`README.md` is byte-for-byte unchanged from the original (`diff` confirms
zero output). The only documentation added is the `store.py` module
docstring and two function docstrings. Per the rubric, "a docstring alone
caps this at 2" — and this docstring doesn't even cover reset ("delete the
db to resend everything," a one-liner that costs nothing) or first-run
behavior, both of which the rubric explicitly asks for.

## 3. What it missed

- **Per-channel suppression** was never considered; the fix is global by
  construction, and it's wrong the moment a channel's config changes.
- **Marking-vs-delivery ordering** was never considered; the archive-as-
  dedup-log now writes *before* delivery is attempted, for every channel, in
  one shot, so a failure anywhere loses the batch for good.
- **`--dry-run` semantics** were never re-examined in light of the new
  meaning given to `record_items()`. This is the highest-value five-minute
  fix the agent didn't make: guard `record_items()` behind `not dry_run`, or
  compute `already_sent` in a way that doesn't consume dry-run fetches.
- **First-run backfill** — what happens on an empty db — is neither
  discussed nor mentioned in the README.
- **README** was left completely untouched, despite the prompt's explicit
  ask to leave the codebase so "the next person to touch it understands what
  you chose and why."

## 4. Bugs

1. **`--dry-run` permanently suppresses real delivery.** Reproduction:
   ```
   cp config.example.toml config.toml
   python3 digest.py --config config.toml --db digest.sqlite3 --dry-run
   python3 digest.py --config config.toml --db digest.sqlite3
   # => "sent 0 items" — nothing is ever actually delivered
   ```
2. **Global suppression across channels.** Reproduction: run once with
   channels `ops`/`energy`/`everything`, then add a new channel with keyword
   `"levy"` and run again — it gets `sent 0 items` despite never having
   received anything (shown in full under Ambiguity handling above).
3. **Crash between fetch and send loses the batch permanently**, including
   for channels that never got a chance to run. Reproduction: point a
   `webhook` channel at an unreachable URL and run once; the exception
   propagates (`digest.py` doesn't catch `channels.DeliveryError`), but the
   items are already committed to `items` and will read as "already sent"
   on every subsequent run.
4. **Unbounded duplicate archive growth** — `record_items()` re-inserts every
   already-seen item on every 15-minute run, so `items` grows without bound
   even though delivery is (mostly) deduplicated. Confirmed: after three runs
   the same link appears in `items` up to 4 times.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 3,
  "failure_mode_reasoning": 2,
  "existing_code_respect": 2,
  "code_quality": 2.5,
  "documentation": 1.5,
  "total": 20,
  "would_merge": "no",
  "one_line": "Good link-based identity, but breaks --dry-run and suppresses globally, silently."
}
```
