# Review — notify-digest, run-3

## 1. Summary

The agent correctly diagnosed that `store.record_items` archived every item on every tick but nothing ever consulted the archive, and fixed it by loading `seen_links()` at the start of each run and filtering against a UTM-stripped "canonical link" identity, which is well chosen and verified by a real test suite against both fixture snapshots. It documented the dedupe key and its rationale clearly in the README, but it left the sent-marker write *before* delivery, so a channel failure now silently and permanently drops the batch instead of retrying it, and it never noticed or discussed per-channel suppression. I would merge this only after fixing the record-before-send ordering — the identity work is good, but as shipped it trades "duplicate spam" for "silent, unrecoverable loss on transient failure," which is the worse failure mode for a notification system.

## 2. Per-category scoring

### Identity strategy — 7/10

The chosen identity is "canonical link" (UTM-stripped), applied uniformly across all three feed formats in `feeds.py`:

```python
_UTM_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign",
    "utm_term", "utm_content", "utm_id",
})

def _canonical_link(url):
    if not url:
        return url
    parsed = urllib.parse.urlparse(url)
    clean_qs = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parsed.query)
        if k not in _UTM_PARAMS
    ]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(clean_qs)))
```

and applied per-format in `fetch()`: `"link": _canonical_link(r["url"])` (newsroom), `_canonical_link(r["permalink"])` (blogroll), `_canonical_link(r["link"])` (wire/generic). This survives all three quirks in the fixtures — I ran it:

- `newsroom`: `utm_campaign=w33 → w34` on re-poll of the same article — resolved (link matches).
- `blogroll`: no `raw_id` at all — permalink used directly, always present.
- `wire`: `guid` regenerated on edit (`wire-2026-08-14-0031` → `-r2`) — link unchanged, so it's correctly treated as the same item.

I confirmed end-to-end against the real fixtures (not just the agent's own tests):

```
$ python3 digest.py --config config-a.toml --db digest.sqlite3        # snapshot-a
sent 12 items
$ python3 digest.py --config config-a.toml --db digest.sqlite3        # same snapshot again
sent 0 items
$ python3 digest.py --config config-b.toml --db digest.sqlite3        # snapshot-b
sent 2 items   # only the genuinely-new "Union responds..." item, to two matching channels
```

This is correct and matches the design intent stated in the README's rationale table. Docked points because the choice's own failure modes aren't stated — only the failure modes of the *rejected* alternatives (`guid`, missing `raw_id`) are discussed. Nothing addresses: what happens if a provider changes its permalink scheme for existing content (silent re-delivery), or if a genuinely different article reuses a link (silent suppression). The `_UTM_PARAMS` list is also hardcoded and just has a comment telling future maintainers to "extend this list" — a fallback (e.g. stripping *any* param matching `^utm_`) would have been more robust than an enumerated set, and it's not discussed why enumeration was chosen over a prefix rule.

### Ambiguity handling — 3/8

Of the three named forks in the rubric:

- **Edited item as new vs. same**: resolved (silently, via the identity choice itself) as "same" — an edited wire item (`guid` bumped, summary text changed to add "Adds operator comment") is never re-sent. This is a reasonable default but is nowhere named as a deliberate decision with its consequence spelled out (i.e., "corrections/updates will not re-notify anyone"). The README explains *why* `guid` was rejected as a key, but never states the resulting policy on edits.
- **First-run backfill**: partially touched. The README warns "Do not clear `digest.sqlite3` in production unless you want every item in the current feeds to be re-delivered as if it were new" — but this addresses *resetting* an existing DB, not the equally real scenario of *first deployment* against feeds that already contain a backlog (a fresh box pointed at live feeds for the first time will blast the entire current backlog in one digest). The agent's own test (`test_first_run_sends_all_items`) proves it knows this happens, but it's never surfaced as a decision in the docs.
- **Per-channel vs. global suppression**: not named at all, and it is silently wrong. Dedup happens once, globally, before the per-channel filter in `digest.py`:

```python
already_seen = store.seen_links(db)
...
new_items = [i for i in items if i["link"] not in already_seen]
store.record_items(db, feed_cfg["name"], new_items)
already_seen.update(i["link"] for i in new_items)
all_items.extend(new_items)
...
for chan_cfg in cfg["channels"]:
    selected = [i for i in all_items if matches(i, chan_cfg)]
```

Suppose an item is recorded (and delivered to whichever channels it currently matches) in run N, and its content is edited in run N+1 such that it *now* also matches a different channel's keywords (or a channel's keyword list is edited to add a term). Because the link is already in the archive, the item never reaches `all_items` again — the new channel never receives it, even though it never received it before. There is no per-(link, channel) delivery record, only a global per-link one. This is exactly the "typically global suppression" failure the rubric calls out, and it is undiscussed.

### Failure-mode reasoning — 2/8

This is the most serious gap. `record_items()` — which is what makes an item "seen" for future runs — is called in the feed-fetch loop, strictly *before* the channel-send loop that can fail:

```python
def run_once(cfg, db, dry_run=False):
    already_seen = store.seen_links(db)
    all_items = []
    for feed_cfg in cfg["feeds"]:
        ...
        new_items = [i for i in items if i["link"] not in already_seen]
        store.record_items(db, feed_cfg["name"], new_items)   # <-- marked "seen" here
        already_seen.update(i["link"] for i in new_items)
        all_items.extend(new_items)
    ...
    for chan_cfg in cfg["channels"]:
        ...
        channels.send(chan_cfg, body)   # <-- can raise DeliveryError, well after the mark above
```

`channels.py`'s own docstring states the intended contract: *"Delivery is best effort: if a channel is down we let the exception propagate and cron will pick us up again on the next tick."* That contract is now broken, because by the time `channels.send` raises, the items have already been marked seen and will **never** be attempted again. I reproduced this directly:

```
$ python3 - <<'EOF'
cfg = {"feeds": [{"name": "wire", "format": "generic", "url": "file://fixtures/snapshot-a/wire.json"}],
       "channels": [{"name": "broken", "type": "webhook", "url": "http://localhost:1/nope", "keywords": []}]}
db = store.connect("digest.sqlite3")
digest.run_once(cfg, db, dry_run=False)
EOF
run_once raised: DeliveryError broken: <urlopen error [Errno 61] Connection refused>
seen_links after failed delivery: {'https://wire.example/i/0918', 'https://wire.example/i/0031'}
```

Both wire items are permanently marked delivered even though delivery never succeeded and the exception propagated exactly as `channels.py` says it should for cron to retry. Given this runs every 15 minutes against real webhooks that will occasionally be down, this silently and permanently drops whatever batch was in flight during the outage — the opposite failure mode from the one the task asked to fix, and arguably worse (duplicates are merely annoying; silent loss is invisible). None of this is discussed anywhere in the README or code comments; the "Deduplication" section only discusses read-side identity, not write-ordering relative to delivery.

### Existing-code respect — 5/6

The agent worked with the grain of the codebase: `feeds.py` keeps its three-format dispatch and just adds one normalising helper; `channels.py` and `render.py` are untouched; `store.py` keeps the same table and adds an index guarded for backward compatibility with pre-existing databases:

```python
try:
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_link ON items(link)")
except sqlite3.OperationalError:
    pass  # Pre-existing duplicates prevent the index; seen_links() still works.
```

That's a reasonable migration story for an existing DB, and `--dry-run` still runs and prints (verified above). No unrelated rewrites, no scope creep. Docked half a point because the record-before-send ordering (see failure-mode section) is a structural issue introduced by *this* change — dedup didn't exist before, so there was no prior ordering to respect, and the agent should have restructured around delivery rather than bolting the seen-check onto the existing fetch-then-send shape unmodified.

### Code quality — 3.5/4

Clean, consistent with existing style, comments explain intent rather than restating code, no dead code or vestigial columns. `INSERT OR IGNORE` plus a Python-level pre-filter is slightly redundant but the comment explains why (race safety for concurrent ticks), which is a fair tradeoff to note rather than a flaw.

### Documentation — 3.5/4

The README gets a full "Deduplication" section (not just a docstring) with a table mapping each feed's quirk to the mechanism that handles it, a note on `--dry-run`'s interaction with the store, and a caution against clearing the DB in production. This clears the "docstring alone caps at 2" floor easily. Not full marks because, as above, it doesn't mention the failure/retry semantics or the per-channel suppression gap — a maintainer reading only the README would not learn about either of the two most consequential unstated decisions.

## 3. What it missed

- **Record-before-send ordering** (see Failure-mode reasoning): should mark items seen *after* a channel's send succeeds, per channel, not once globally before any send is attempted.
- **Per-channel suppression**: no per-(link, channel) tracking; a link that becomes newly relevant to a channel after being suppressed elsewhere will never reach it.
- **`--dry-run` still mutates the store.** The agent kept the original unconditional `store.record_items()` call (now consequential, since it feeds the dedup set) and documented the effect: *"nothing is delivered and the database is still updated, so items seen in a dry run will not appear in the next real run."* This is at least transparently documented, but it's a real footgun for anyone using `--dry-run` to "try it out" against the fixtures as the README's own "Layout" section recommends — a dry run permanently poisons production dedup state for those items.
- **Edited-item semantics** aren't named as a decision anywhere, only implied by the identity mechanism.
- **First-deploy backlog blast** isn't distinguished from "resetting the DB" in the docs, even though the agent's own test proves the behavior exists.

## 4. Bugs

**Confirmed: transient delivery failure permanently drops the batch, contradicting the codebase's own documented retry contract.**

Reproduction (run from the repo root with a channel pointed at a closed port):

```python
import digest, store
cfg = {"feeds": [{"name": "wire", "format": "generic", "url": "file://fixtures/snapshot-a/wire.json"}],
       "channels": [{"name": "broken", "type": "webhook", "url": "http://localhost:1/nope", "keywords": []}]}
db = store.connect("digest.sqlite3")
digest.run_once(cfg, db, dry_run=False)   # raises DeliveryError, as channels.py intends
store.seen_links(db)  # -> {'https://wire.example/i/0918', 'https://wire.example/i/0031'}
```

Both items are in `seen_links()` despite delivery never succeeding. The next cron tick (15 minutes later, per the deployment crontab in the README) will not retry them — they are gone permanently, with no log line indicating loss. Given webhooks going down transiently is exactly the scenario `channels.py`'s docstring was written to handle, this is a real regression introduced by the fix, not a pre-existing issue.

No other outright breakage found; the core dedup path (the thing actually asked for) works correctly and is well covered by `tests.py`, which I ran unmodified and which passes (15/15).

```json
{
  "identity_strategy": 7,
  "ambiguity_handling": 3,
  "failure_mode_reasoning": 2,
  "existing_code_respect": 5,
  "code_quality": 3.5,
  "documentation": 3.5,
  "total": 24,
  "would_merge": "yes-with-fixes",
  "one_line": "Good link-based dedupe, well tested, but marks items sent before delivery succeeds."
}
```
