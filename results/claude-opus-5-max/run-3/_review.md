# Review

## 1. Summary

The agent introduced a dedicated `identity.py` module that keys items on a tracking-parameter-stripped canonical link (with a scoped raw-id and content-hash fallback), added a `deliveries(channel, item_key)` table written only after a successful send, and fixed the unconditional archive insert with a `WHERE NOT EXISTS` guard plus an in-place schema migration for existing databases — all while leaving `feeds.py`, `channels.py`, `render.py`, and `config.example.toml` untouched. It backed the change with 33 passing unit tests built directly on the two fixture snapshots, and a README section that walks through the identity choice, the three ambiguity forks, and deployment/backfill guidance. I would merge this: it is correct on all three fixture feeds, per-channel/per-delivery failure semantics are sound and tested, and the reasoning is written down where the next person will actually find it.

## 2. Per-category scoring

### Identity strategy — 9/10

The strategy is a single fallback chain — canonical link, then scoped raw_id, then a content hash — argued explicitly against a table of what breaks on each feed:

```python
# identity.py
def item_key(item):
    link = (item.get("link") or "").strip()
    if link:
        return "link:" + canonical_link(link)
    raw_id = (item.get("raw_id") or "").strip()
    if raw_id:
        return "raw:{}:{}".format(item.get("source", ""), raw_id)
    material = "\n".join((item.get("source") or "", item.get("title") or "", item.get("published") or ""))
    return "sha:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
```

`canonical_link` strips `utm_*` and a fixed set of click-id params, lowercases scheme/host, trims trailing slash, and sorts query params — enough to survive the newsroom `utm_campaign` rotation without touching `?id=`-style content params. I verified this end-to-end against the fixtures (see below): running snapshot-a then snapshot-b sends exactly one new item, "Union responds to port fee inquiry" — the newsroom URL-rotation, the wire `-r2` guid, and the blogroll retitle/edit are all correctly suppressed. The docstring in `identity.py` states the failure mode it accepts (an edit is never re-sent) and the one it explicitly rejects (per-provider special-casing). This is very close to top-band; the only gap is that a genuinely *new* item on a link-less feed that happens to collide in title/source/day would silently merge (acknowledged in a comment but not tested), keeping it at 9 rather than 10.

### Ambiguity handling — 8/8

All three named forks in the rubric are identified and resolved in both code and prose:

- **Per-channel vs. global suppression** — `store.py`'s `deliveries` table is keyed `(channel, item_key)`, with the reasoning written next to the schema: "A single global 'sent' flag would let whichever channel was processed first consume the item and starve the rest." Tested in `test_overlapping_channels_both_receive_a_matching_item`.
- **Edited item as new vs. same** — decided explicitly: "An edit is not a new item... That is the intended product behaviour: readers complained about repeats, not about missing errata" (`identity.py` docstring, mirrored in README). Tested by `test_retitled_and_edited_post_is_not_a_new_item`.
- **First-run backfill** — named and given an escape hatch: a channel added later gets the full current backlog by design, with `--mark-seen` provided and tested (`test_channel_added_later_receives_the_backlog`, `test_mark_seen_adopts_the_backlog_without_sending`) for when that's not wanted. The README even documents the exact one-time command to run before the first cron tick after deploying.

### Failure-mode reasoning — 8/8

Per-channel marking, written only after successful delivery, with the ordering and the guarantee argued:

```python
# digest.py run_once()
try:
    channels.send(chan_cfg, body)
except channels.DeliveryError as exc:
    print(f"warn: channel {name} failed: {exc}", file=sys.stderr)
    continue
store.mark_sent(db, name, selected)
```

```python
# store.py mark_sent docstring
"""Record that `channel` has been sent `items`.
Call this only after delivery succeeds. Marking first would mean a
channel outage silently ate the items it failed to deliver.
"""
```

This gives at-least-once delivery per channel: channel two failing after channel one succeeds leaves channel one's items marked and channel two's items retried next tick, independently. I confirmed this in the test suite (`test_one_failing_channel_does_not_block_the others`, `test_healthy_channels_are_not_re_sent_while_one_recovers`) rather than just reading it. The tradeoff (retry-until-delivered, never silently drop) is stated in both the `run_once` docstring and the README's "Deliberate choices" section.

### Existing-code respect — 6/6

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are byte-for-byte identical to the original (verified with `diff`). The `items` archive is kept and its "did this ever come through" purpose preserved; the unconditional-insert problem it names in the rubric is fixed with a `WHERE NOT EXISTS` guard rather than a rewrite, and a real migration path is provided and tested (`TestLegacyDatabaseMigration`) for databases that already have the old duplicate-laden schema. `--dry-run` still works and is explicitly kept side-effect-free (`test_dry_run_is_repeatable`). No scope creep.

### Code quality — 4/4

No dead code; the vestigial-columns risk (old `items` schema evolving) is handled with a real `ALTER TABLE` migration rather than a fresh `CREATE TABLE`, and it's guarded by `PRAGMA table_info`. SQL is parameterized throughout, and the `INSERT ... SELECT ... WHERE NOT EXISTS` guard is explained inline rather than left mysterious. `--mark-seen` and `--dry-run` are made mutually exclusive with a clear `ap.error(...)` rather than an ambiguous silent precedence.

### Documentation — 4/4

The README dedicates a full "How repeats are avoided" section to the dedupe behaviour, a table of which field breaks on which feed, an explicit "What counts as 'the same item'" walkthrough, and a documented reset/backfill path (`--mark-seen`) plus the exact deployment sequence for the first run after upgrading ("The first run after deploying this change sends one last digest of whatever is currently in the feeds... To skip that, prime it once before the next tick"). This goes well beyond a docstring — it's the kind of doc a second reader can act on without reading `identity.py`.

## 3. What it missed

- **Content-hash fallback is unaddressed for real ambiguity**: a link-less, id-less item whose title/source/date collides with another item on the same day would merge silently (rare in these three feeds, but the code doesn't guard or test for it — it's mentioned only in a comment as an accepted risk).
- **Tracking-param allowlist is a moving target**: the `_TRACKING_PARAMS` set is a hardcoded list of known ad-network click IDs; a new tracking parameter introduced by a provider later would leak into the key and cause a fresh burst of "new" items. This is a reasonable simplicity tradeoff but isn't flagged as something to revisit.
- **No CLI to inspect/reset delivery state** beyond `--mark-seen` — there's no `--reset` or query tool to answer "why did this get sent again" if the key logic changes later (e.g. tracking-param set updated), though the README does explain that changing canonicalisation will cause a one-time resend, which is honest and sufficient for this scope.

## 4. Bugs

None found. I ran the actual binary end-to-end:

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # same snapshot again
sent 0 items
$ sed -i '' 's/snapshot-a/snapshot-b/' config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
  ...
sent 2 items
```

Only the genuinely new item ("Union responds to port fee inquiry") was sent on the snapshot-b run; the utm-rotated, guid-regenerated, and retitled items were all correctly suppressed. `python3 -m unittest` passes all 33 tests (`Ran 33 tests in 0.068s / OK`).

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 4,
  "total": 39,
  "would_merge": "yes",
  "one_line": "Fallback link-canonicalisation identity, per-channel delivery table, migration, tests, and clear README."
}
```
