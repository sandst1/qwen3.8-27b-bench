# Review: notify-digest dedup fix

## 1. Summary

The agent introduced a `identity.py` module that keys items on a
tracking-parameter-stripped link (with `raw_id` and a title+published hash as
fallbacks), added a per-channel `deliveries` ledger in `store.py`, wired
`digest.py` to skip already-delivered keys and mark them only after a
successful send, and handled the upgrade of an existing (pre-dedup) database
so it doesn't re-blast its whole archive on the first post-fix run. I ran it
against both fixture snapshots, a hand-built legacy database, and a
simulated mid-run channel failure; in every case it behaved exactly as the
code and README claim. I would merge this — with a follow-up to add the
"how do I reset this" note the README is missing and to make
`identity.normalize_link` deterministic under query-parameter reordering.

## 2. Per-category scoring

### Identity strategy — 9.5 / 10

`identity.py:50-61` keys on the normalised link, with `raw_id` and a
title+published hash as explicit last resorts:

```python
def item_key(item):
    """Return the dedup key for a normalised feed item."""
    source = item["source"]
    link = item.get("link")
    if link:
        return f"{source}|{normalize_link(link)}"
    if item.get("raw_id"):
        return f"{source}#raw|{item['raw_id']}"
    digest = hashlib.sha256(
        f"{item['title']}|{item.get('published') or ''}".encode()
    ).hexdigest()
    return f"{source}#hash|{digest}"
```

`normalize_link` strips `utm_`/`mc_`/`ref*` query params and the fragment
(`identity.py:31-47`). I verified this against all three fixture quirks by
running snapshot-a then snapshot-b through the real code:

- **newsroom** rotates `utm_campaign` (`w33` → `w34`) on *every* item,
  including ones that didn't change. Confirmed not re-sent.
- **wire**'s guid for the port-fee item goes from `wire-2026-08-14-0031` to
  `...-0031-r2` between snapshots (an edit). Confirmed not re-sent.
- **blogroll** has no id at all (`raw_id: None` always, `feeds.py:71`) and
  its port-fee-arithmetic post gets a retitle + rewritten excerpt in
  snapshot-b. Confirmed not re-sent (link-only identity, exactly as
  documented).

Only the genuinely new newsroom item (`entry_id 84130`, a different URL
slug) came through on the snapshot-b run:

```
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
  ...port-fees-union?utm_source=feed&utm_campaign=w34
sent 2 items
```

That's the top band of the rubric: a fallback chain that survives all three
fixtures, with failure modes stated in the module docstring
(`identity.py:1-21`, e.g. "Fallbacks... are last resorts, not good
identities — the wire guid churn shows why").

Docked half a point for two un-exercised but real gaps in `normalize_link`:
it doesn't sort the surviving query parameters, so `?a=1&b=2` and `?b=2&a=1`
produce different keys for what could be the same URL —

```
$ python3 -c "import identity; print(identity.normalize_link('https://x/s?b=2&a=1')); print(identity.normalize_link('https://x/s?a=1&b=2'))"
https://x/s?b=2&a=1
https://x/s?a=1&b=2
```

— and `_TRACKING_PREFIXES = ("utm_", "mc_", "ref")` will also strip a
legitimate param like `reference_id` (`normalize_link('...?reference_id=1')`
→ `https://example.com/story`, param silently gone). Neither is hit by the
fixtures, but a future feed could regress silently.

### Ambiguity handling — 7.5 / 8

All three named forks from the rubric are explicitly resolved, in both code
comments and the README, not just picked silently:

- **Per-channel vs. global suppression** — `store.py:35-40`, `deliveries`
  is keyed `(channel, dedup_key)`, and the reasoning is in the docstring:
  "We track per channel rather than globally because channels filter
  differently — an item is 'new' for a channel until *that channel* has
  received it." I confirmed this empirically: the `ops`/`energy`/`everything`
  channels each got their own independent copy of overlapping items on the
  first run, and each independently stopped re-sending them afterward.
- **Edited item as new vs. same** — decided and stated twice, in
  `identity.py:14-16` and in the README ("An edited story is not re-sent;
  people complained about duplicates far more than they asked for update
  notices"). Verified above with the blogroll retitle and wire guid churn.
- **First-run backfill** — this is a live production job ("we run
  digest.py from cron"), so the operative first-run case is upgrading an
  *existing* database, which the agent handled directly: `store.py:71-100`
  flags a legacy DB via a `meta` row and `digest.py:81-86` seeds the
  `deliveries` ledger from the existing archive before the first tick runs,
  so the upgrade doesn't re-blast everything already seen. I built a
  synthetic legacy database (old schema, no `dedup_key`) and confirmed items
  already in the old archive were suppressed while genuinely-new items
  still went out — see the Bugs section for the exact run.

Half a point off because the *other* first-run case — a brand-new
deployment with no prior database at all — isn't discussed anywhere; it
silently falls out of the same machinery (everything currently live in the
feed goes out once). That's a defensible default, but it's a real decision
("day-one install floods every channel with whatever's currently live")
that never gets named the way the upgrade case does.

### Failure-mode reasoning — 7 / 8

Per-channel marking, correct order, argued in both code and README:

```python
# digest.py:57-66
body = render.digest(selected, chan_cfg)
if dry_run:
    print(f"--- would send to {chan_cfg['name']} ---")
    print(body)
    continue
channels.send(chan_cfg, body)
# Recorded only after a successful send: if delivery blows up, the
# next cron tick retries these items.
store.mark_delivered(db, chan_cfg["name"], (i["key"] for i in selected))
sent += len(selected)
```

I reproduced a mid-run partial failure with three channels, the middle one
a webhook to a closed port:

```
=== first ===          # stdout, succeeds
[... 2 items ...]
Traceback ...
channels.DeliveryError: broken: <urlopen error [Errno 61] Connection refused>
exit code: 1
```

`first` was marked delivered before the crash; `third` never even ran this
tick, because the exception is allowed to propagate out of `run_once`
(unchanged from the original `channels.py` design: "we let the exception
propagate and cron will pick us up again"). I then "fixed" the broken
channel and re-ran: `first` correctly did **not** get a duplicate, and
`third` correctly received the items it missed. That's a sound at-least-once
choice — no batch is lost, no already-succeeded channel is duplicated — but
it is inherited from the pre-existing control flow, not something the agent
added, and neither the code comments nor the README call out the resulting
"a channel two failure delays channel three by one tick" behavior or name
the at-least-once/at-most-once choice in those terms. The mechanism is
right and I could reconstruct the guarantee by testing it; the argument for
it is implicit rather than spelled out.

### Existing-code respect — 6 / 6

The diff is minimal and additive. `channels.py` and `render.py` are
untouched. `feeds.py`'s per-format branches are unchanged except for
tagging each item with `item["key"] = identity.item_key(item)` at the end
(`feeds.py:91-93`); the three `if/elif/else` bodies are otherwise identical
to the original. `digest.py`'s loop structure and CLI are untouched apart
from the ledger check/mark calls. `--dry-run` still works and still prints
`sent 0 items`, matching the original semantics (the `continue` before
`channels.send`/`mark_delivered` is unchanged in spirit).

The rubric specifically flags "reusing the `items` archive is fine *if* the
unconditional insert is dealt with" — it is:

```python
# store.py:123-141
"INSERT OR IGNORE INTO items"
" (source, raw_id, title, link, summary, published, dedup_key)"
```

backed by a unique index (`idx_items_dedup`) added via migration, plus a
backfill (`_migrate_backfill`, `store.py:103-120`) that assigns keys to rows
written by the old code and safely leaves exact-duplicate legacy rows with a
`NULL` key rather than crashing:

```
$ sqlite3 legacy.sqlite3 "select id, dedup_key from items order by id"
1|newsroom|https://newsroom.example/2026/08/port-fees
2|                                    <- duplicate row, left NULL, no crash
3|wire|https://wire.example/i/0031
```

### Code quality — 3.5 / 4

Readable, well-commented, migration ordering is explained where it matters
("Indexes before the backfill: UPDATE OR IGNORE... relies on the unique
index to skip duplicate rows," `store.py:63-64`) and I verified that
ordering claim is actually true by building a legacy DB with a duplicate row
and watching it resolve correctly. No vestigial columns were introduced;
`raw_id` stays for archive/debugging value, not as clutter.

Docked half a point for one real inefficiency: `_migrate_backfill` runs its
`SELECT ... WHERE dedup_key IS NULL` query on *every* `connect()` call, not
just once after a genuine upgrade. For a legacy DB containing exact-duplicate
rows (which can never get a unique key and will always fail the
`UPDATE OR IGNORE`), this means the same doomed update is retried on every
single cron tick, forever — harmless, but wasted work with no exit
condition, and undocumented as such.

### Documentation — 3 / 4

The README's new "Deduplication" section (`README.md:28-58`) is substantial,
not a docstring-only afterthought, and explicitly covers two of the three
things the rubric asks for:

```markdown
Why the key is the *normalised link* ... rather than an id — the three
providers each break a different "obvious" choice: ...

Consequences, deliberately chosen:
* An edited story is **not** re-sent...
* The ledger is **per channel**...
* Sends are recorded **after** a successful delivery...

Old databases are upgraded automatically on first run: keys are backfilled
onto archived items, and ... the ledger is seeded from the archive so the
upgrade itself does not re-blast anything.
```

Dedupe behavior: covered in depth. What happens on first run: covered in
depth (for the realistic upgrade case). Missing entirely: **how to reset
it**. There is no mention anywhere of deleting `digest.sqlite3`, truncating
`deliveries`, or how an operator would force a specific item (or channel) to
be re-sent. That's one of the three explicit asks in the rubric's
documentation category and it's simply absent.

## 3. What it missed

- **How to reset dedup state.** Not in the README, not a CLI flag. An
  operator's only option, unstated, is to manually open the sqlite file and
  `DELETE FROM deliveries` or delete the whole db.
- **Query-parameter order stability** in `normalize_link` — not a fixture
  bug, but a latent one (shown above).
- **The `ref` prefix** in `_TRACKING_PREFIXES` is broader than intended and
  would silently swallow a param like `reference_id` if a future feed used
  one as an identifier.
- **Brand-new install first run** (no prior database) isn't named as a
  decision anywhere, even though it's the other half of the "first-run
  backfill" fork the rubric asks about; the agent only wrote about the
  upgrade-from-legacy-db case.
- **The "channel two fails, channel three doesn't even run this tick"
  consequence** of leaving the uncaught-exception control flow in place
  isn't discussed, even though the agent's own throwaway testing (visible in
  `_agent.log`) explicitly exercised a webhook failure and checked the
  `deliveries` table afterward — it verified the behavior but didn't write
  down what it verified.

## 4. Bugs

None that cause incorrect delivery, duplicate sends, or data loss under any
scenario in the fixtures, a synthetic legacy database, or a synthetic
mid-run channel failure that I tried. The closest things to real defects are
the two identity-fragility points above (query-param ordering, `ref`
over-matching), which are real but not currently triggered by any of the
three feed formats in this repo, and the wasted-but-harmless repeated
backfill scan on every connect for un-resolvable legacy duplicate rows.

Reproduction of the query-param-order issue (the only one I'd call a latent
bug rather than a documentation gap):

```
$ python3 -c "
import identity
print(identity.normalize_link('https://example.com/story?b=2&a=1'))
print(identity.normalize_link('https://example.com/story?a=1&b=2'))
"
https://example.com/story?b=2&a=1
https://example.com/story?a=1&b=2
```

If a provider ever reorders non-tracking query parameters between fetches of
the same story, this dedup key changes and the "same item" would be treated
as new — the exact class of bug this whole fix exists to prevent, just with
a different trigger than the ones in the fixtures.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 7.5,
  "failure_mode_reasoning": 7,
  "existing_code_respect": 6,
  "code_quality": 3.5,
  "documentation": 3,
  "total": 36.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Well-tested link-based dedup with per-channel ledger; README never says how to reset it."
}
```
