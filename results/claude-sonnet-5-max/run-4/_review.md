# Review — notify-digest, run-4

## 1. Summary

The agent introduced a per-channel `deliveries` table keyed on a new `dedup_key` — a
tracking-parameter-stripped, source-prefixed normalized link, computed once in `feeds.py`
and consumed by `digest.py`/`store.py` — replacing the previous no-op archive as the
dedup mechanism, and wrote up why raw provider ids were rejected for each of the three
feed formats. It correctly survives all three fixture quirks (rotating `utm_campaign`,
a missing `raw_id`, a regenerated `guid`), keeps `--dry-run` and the existing archive
table intact, and marks delivery per-channel only after a successful send. I would
merge this, with a request to add a note on first-run/backfill behavior and how to
reset the dedup state, since both are absent and the resulting behavior (silently
sending an entire feed's backlog on first deploy) is worth a sentence.

## 2. Per-category scoring

### Identity strategy — 9/10 (of 10)

Single, well-reasoned strategy: normalize the link (strip tracking params, sort
remaining query, drop nothing else) and prefix with source name. `feeds.py:44-63`:

```python
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid",
}

def _normalize_link(url):
    parts = urlsplit(url)
    kept = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))

def _dedup_key(source, link):
    return f"{source}:{_normalize_link(link)}"
```

I verified this against the fixtures directly. Running `snapshot-a` then switching
config to `snapshot-b` (simulating the next poll):

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # again, same snapshot
sent 0 items
$ sed -i '' 's/snapshot-a/snapshot-b/g' config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-b
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
=== everything ===
Firehose — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```

Only the genuinely new "Union responds..." newsroom entry went out, despite:
- `newsroom` rotating `utm_campaign=w33` → `w34` on every other repeated entry
  (`fixtures/snapshot-b/newsroom.json`),
- `wire`'s `wire-2026-08-14-0031` guid becoming `wire-2026-08-14-0031-r2`
  (`fixtures/snapshot-b/wire.json`) with the same `link`,
- `blogroll` having no id at all and its stable-link entry getting its title/excerpt
  edited (`fixtures/snapshot-b/blogroll.json`, title becomes "(updated)").

None of these three were re-sent. The rationale is stated with an explicit failure
mode, in `feeds.py`'s module docstring and echoed in the README (`README.md:72-74`):

> If a feed ever starts giving you a genuinely stable, edit-surviving id, prefer that
> over the link-based key for that format — the link heuristic is a good general
> fallback, not a law of nature.

This is not a per-feed fallback chain (it's one rule applied uniformly), but it is
demonstrably correct against all three fixtures and the tradeoff is named, which is
why I'm not going all the way to 10 — a true fallback chain (prefer stable id when the
provider has one, e.g. `wire`'s guid *before* the `-r2` suffix appears, falling back to
link only when needed) would be slightly more robust to the case of two genuinely
different stories that happen to share a path after query stripping. That's a
theoretical gap the fixtures don't expose.

### Ambiguity handling — 5/8 (of 8)

Per-channel vs. global is explicitly identified and resolved, in prose, with the
reasoning stated (`README.md:47-50`):

> **Why per-channel, not global.** The same item can match more than one channel's
> keywords. That's not a duplicate — it's two different audiences who both care. So
> "have we sent this" is scoped to `(channel, dedup_key)`, not just `dedup_key`.

I confirmed this behaviorally: on the first run against `snapshot-a`, the same
"Notes on port fee arithmetic" blogroll item went to both `ops` and `everything`
independently in the same tick (both channels' output above), which is correct.

Edited-item-as-new-vs-same is decided (link-based dedupe implicitly treats edits as
"same item"), and the wire `-r2` guid is used as the illustrating example in the
README, but the decision is never framed as its own fork with its consequence spelled
out — namely, that a factual correction to an already-sent item (the blogroll excerpt
correction, or the wire "Adds operator comment" edit) will never reach anyone who
already got the earlier version. That's a real operational tradeoff for a
notification system and it goes unstated.

First-run/backfill is not mentioned anywhere — not in the README, not in a comment.
On a fresh `deliveries` table (first deploy, or after restoring from backup, or a
config change that points at a feed with a long history), every currently-matching
item across every channel will go out in the very first tick with no throttling or
warning. I confirmed this is exactly what happens (see the `sent 12 items` first run
above, using nothing but two feed snapshots with 4-6 stable items apiece — a live feed
with a real backlog would flood every channel identically). This is a straightforward
one-paragraph README addition the agent skipped.

One clearly-named fork (per-channel) plus one addressed-but-unframed fork
(edit=same) plus one wholly silent fork (backfill) lands this at the middle of the
"4–6: one or two named, rest silent-but-correct" band — silent-but-correct is a fair
characterization of edit-as-same and of backfill (nothing in the fixtures suggests
either choice is wrong), but the coverage is short of "all three… explicitly."

### Failure-mode reasoning — 7/8 (of 8)

Per-channel marking, done strictly after a successful send, `digest.py:64-74`:

```python
body = render.digest(selected, chan_cfg)
if dry_run:
    print(f"--- would send to {chan_cfg['name']} ---")
    print(body)
    continue
channels.send(chan_cfg, body)
# Only mark delivered *after* a successful send. If channels.send
# raises, these items stay "undelivered" and will be retried next
# tick, same as the existing best-effort delivery contract.
store.mark_delivered(db, chan_cfg["name"], (i["dedup_key"] for i in selected))
sent += len(selected)
```

I forced a failure (pointed `ops` at a webhook with nothing listening) to check the
cross-channel window explicitly:

```
EXCEPTION: DeliveryError('ops: <urlopen error [Errno 61] Connection refused>')
deliveries after failure: []
```

`ops` raises before it's marked, so it's correctly retried later — no duplicate risk
for `ops` itself. But there's no try/except around the per-channel loop in
`run_once`, so the *entire run_once call* aborts on the first channel that raises:
`energy` and `everything`, which come after `ops` in `cfg["channels"]`, are never
attempted this tick even though they don't depend on `ops` at all. I confirmed the
recovery path — once the channel is fixed, the next tick sends all three correctly
with nothing lost or duplicated:

```
$ python3 digest.py ...   # ops fails first
EXCEPTION ...
$ python3 digest.py ...   # ops fixed, retry
sent on retry 12
```

So the guarantee is: at-least-once, no double-sends, but a single failing channel
delays every channel ordered after it in `cfg["channels"]` until the next tick — not
just the failing one. This ordering was already implicit in the pre-existing
`channels.py` design ("best effort... cron will pick us up again on the next tick",
`channels.py:5`) and the agent's per-channel marking is consistent with it and
explicitly discussed in the README (`README.md:66-70`). It stops short of full marks
because the cross-channel delay side-effect (as opposed to just "failed channel gets
retried") isn't called out — a one-line comment noting that a broken channel early in
the config list stalls channels after it would close this out.

### Existing-code respect — 6/6 (of 6)

`feeds.py` keeps its existing three `_load`-style shapes and only adds a `dedup_key`
field to the returned dicts plus two small pure helper functions
(`_normalize_link`, `_dedup_key`) — no rewrite. `store.py` keeps the original `items`
schema and `record_items`/`count_items` untouched, and adds `deliveries` as a second
table with `CREATE TABLE IF NOT EXISTS`, which is the correct migration shape for an
already-running SQLite file: existing databases pick up the new table on next
`connect()` with zero manual intervention (verified: reused the same `.sqlite3` file
across all runs above without error).  `--dry-run` was explicitly preserved and its
existing semantics extended correctly — I confirmed two consecutive `--dry-run`
invocations produce byte-identical output (no delivery-state mutation), matching the
new README note (`README.md:13-16`). No scope creep — no new CLI flags, no plugin
system, nothing beyond what the dedup fix required.

### Code quality — 4/4 (of 4)

Clean, small diff. No dead code, no vestigial columns (`deliveries` has exactly the
three columns it needs: `channel`, `dedup_key`, `sent_at`). SQL is straightforward and
parameterized (`store.py:76-79`, `store.py:86-87`); the dynamic `IN (?,?,...)`
placeholder construction is standard practice for a Python/sqlite3 batch lookup and is
scoped to a config with a handful of feeds/channels, not user input. Function and
comment scope stayed tight to the change being made.

### Documentation — 2.5/4 (of 4)

Substantial README section beyond a docstring — a "## Deduplication" section
(`README.md:35-74`) explaining what the key is, why it's per-channel not global, why
it isn't the raw provider id (with per-format examples pointing at the actual fixture
files), and the failed-send retry contract. That's real, specific documentation, not
a docstring stub, so it's above the "docstring alone caps at 2" floor.

It misses two of the three things the rubric explicitly asks for: there is no
"how to reset it" instruction (a fresh deploy or someone needing to force a resend has
no documented path — `DELETE FROM deliveries` would work but isn't mentioned anywhere)
and no "what happens on first run" discussion (see Ambiguity handling above — this is
the same gap scored from a documentation angle: nothing tells an operator that a fresh
`deliveries` table means the very next tick will flush the feeds' entire current
contents to every channel).

## 3. What it missed

- **First-run backfill.** Never discussed. A brand-new deploy, or a restore from
  backup, sends everything currently in every feed to every channel on tick one, with
  no flag or note warning an operator this will happen.
- **How to reset delivery state.** No documented `DELETE FROM deliveries` (or
  equivalent) for someone who legitimately wants a channel to re-receive a backlog.
- **Edited-item semantics as a named tradeoff.** The mechanism (link-based identity)
  silently decides "edited = same item, don't resend," which means corrections
  (the blogroll excerpt fix, the wire "Adds operator comment" edit in the fixtures)
  never reach people who got the pre-edit version. This is mentioned as an example of
  why raw ids were rejected, but never framed as its own decision with its own
  consequence.
- **Cross-channel stall on failure.** A channel failing doesn't just delay itself —
  it aborts the rest of that tick's channel loop (verified above), delaying
  independent channels too. Not called out as a side effect of the loop structure.
- **No test suite added or exercised.** There's no automated check that dedup
  actually holds across the fixtures — I had to run it manually to confirm the claims
  in the README/docstrings are true. (Not explicitly required by the rubric, but
  worth noting: the correctness claims rest entirely on manual verification.)

## 4. Bugs

None found. I exercised:
- same-snapshot double run → 0 resent (`sent 0 items`);
- snapshot-a → snapshot-b transition → only the genuinely new item resent, all three
  fixture-specific churn cases (rotating `utm_campaign`, regenerated `guid`, edited
  no-id blogroll post) correctly suppressed;
- per-channel independence (`ops`/`everything` both correctly received the same
  overlapping item once each in the same tick);
- `--dry-run` idempotence (two consecutive dry runs produce identical output, no
  state mutated);
- forced channel failure followed by retry (failing channel's items stay unmarked and
  are correctly resent next tick; no duplicates once fixed).

All behaved as documented. The only issue found (cross-channel stall on a mid-list
channel failure) is a real but pre-existing-design-consistent side effect, not a
regression or a correctness bug — see Failure-mode reasoning above.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 5,
  "failure_mode_reasoning": 7,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 2.5,
  "total": 33.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid link-based per-channel dedupe; verified correct, but silent on backfill and reset."
}
```
