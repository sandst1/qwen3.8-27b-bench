# Review: notify-digest dedup fix

## 1. Summary

The agent replaced "resend everything every tick" with a per-channel `deliveries`
ledger keyed on a canonicalized article URL (stripping `utm_*`/`fbclid`/`gclid`),
wrapped each channel's select→send→record in a `BEGIN IMMEDIATE` transaction to
survive overlapping cron runs, and wrote an 11-test suite plus a long README
section arguing the identity, ambiguity, and failure-mode choices explicitly.
The mechanism is real and well-tested — I reproduced correct dedupe across both
fixture snapshots and correct locking across two actual concurrent `python3
digest.py` processes — but I also reproduced a latent identity bug (a new,
unlisted tracking parameter resurrects an already-delivered item) and a
channel-ordering bug (one permanently-failing channel silently starves every
channel listed after it in `config.toml`, forever, with no mention of this in
the otherwise thorough failure-mode writeup). I would merge this with fixes:
the architecture and documentation are good enough to build on, but the
tracking-param allowlist and the channel-starvation behavior need to be
addressed or at least disclosed before this runs unattended in production.

## 2. Per-category scoring

### Identity strategy — 7 / 10

The core function, `store.py:58-72`:

```python
def item_key(item):
    """Use the article URL, not editable text or provider-generated GUIDs.

    Remove known tracking parameters and fragments, but retain meaningful
    query parameters (e.g. ?id=123), paths and trailing slashes.
    """
    url = urlsplit(item["link"])
    query = [
        (key, value)
        for key, value in parse_qsl(url.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path,
                       urlencode(query), ""))
```

I ran this against both fixture snapshots and it does survive the three
scenarios the fixtures are built to break on:

- **Rotating `utm_campaign`** (newsroom, `entry_id` 84121/84118, `w33`→`w34`):
  correctly suppressed on the snapshot-b poll — verified by running the CLI
  twice (`snapshot-a` then `snapshot-b`): only the genuinely new `entry_id
  84130` ("Union responds to port fee inquiry") came through.
- **Missing `raw_id`** (blogroll, always `None`): the permalink is stable, so
  the edited post ("Notes on port fee arithmetic (updated)") was correctly
  *not* resent.
- **Regenerated `guid`** (wire, `wire-2026-08-14-0031` → `...-0031-r2`): the
  `link` is stable, so the edited wire item was correctly *not* resent.

The README states this choice and some of its failure modes (`store.py` /
`README.md`: "No redirects are resolved for identity," "We deliberately avoid
fuzzy title matching"). That argumentation is genuinely good and is why this
isn't scored lower.

But the strategy throws away the one truly stable identifier the codebase
already computes for two of the three feeds — `raw_id` (newsroom's `entry_id`,
wire's `guid`-before-edit) — in favor of a hand-maintained tracking-parameter
denylist. That denylist is a guess about what future tracking params will look
like, and it's wrong the moment the provider adds one that isn't `utm_*`,
`fbclid`, or `gclid`. I reproduced this directly:

```
$ # snapshot-a already delivered. Now mutate newsroom entry 84121's URL by
$ # appending a parameter NOT in the strip-list — nothing else changes,
$ # same entry_id, same title, same content:
$ python3 -c "
d = json.load(open('fixtures/snapshot-a/newsroom.json'))
d['entries'][0]['url'] += '&ref=newsletter'
json.dump(d, open('fixtures/snapshot-c/newsroom.json','w'))
"
$ python3 digest.py --config config-c.toml --db digest.sqlite3
=== ops ===
Ops digest — 1 item(s)
* Regulator opens inquiry into port fees  [newsroom]
  https://newsroom.example/2026/08/port-fees?utm_source=feed&utm_campaign=w33&ref=newsletter
sent 2 items
```

Same `entry_id` (84121), same title, same content — resent anyway, because the
URL gained a parameter the allowlist doesn't know about. This is exactly the
bug the task asked to fix, reintroduced through a different door, on a feed
that already had a perfectly reliable `raw_id`. A fallback-chain identity
(`raw_id` when present, canonical URL otherwise) would have been both simpler
for newsroom/wire and immune to this. This un-discussed breakage on a feed
that *does* have a stable id is what keeps this out of the 9-10 band and lands
it at "a single strategy that works for most items; the case it breaks on is
... unnoticed."

### Ambiguity handling — 8 / 8

All three forks named in the rubric are explicitly identified and resolved in
`README.md`, not left implicit:

- **Per-channel vs. global suppression** — "Delivery history is per channel,
  not global. An item sent to `ops` remains eligible for `energy`." Verified:
  `store.py:75-94`'s `pending_items` and the `deliveries` PRIMARY KEY
  `(channel, source, item_key)` both key on channel.
- **Edited item as new vs. same** — "A new article URL is considered new;
  edits at the same URL are not sent again to a channel that already received
  it... We deliberately avoid fuzzy title matching." Verified against both
  the blogroll and wire edits above.
- **First-run backfill** — "### Existing databases": "The first upgraded run
  sends all currently matching items once, then subsequent runs send only
  unsent items. This explicit baseline avoids silently losing items based on
  guessed history." Verified by `test_digest.py:102-124`
  (`test_legacy_archive_is_not_treated_as_delivery_history`) and by hand.

This is exactly the top-band description: all three forks named, and each
decision is stated with its reasoning, not just implemented silently.

### Failure-mode reasoning — 6 / 8

The mechanism is sound and I verified it two ways beyond the agent's own
mocked unit tests. First, a real multi-process test (two actual
`python3 digest.py` invocations racing against the same db file, with a
webhook that sleeps 2s before responding):

```
$ python3 digest.py --config config2.toml --db digest.sqlite3 &   # process A
$ sleep 0.2; python3 digest.py --config config2.toml --db digest.sqlite3 & # process B
sent 2 items     # A
sent 0 items     # B
HITS 1           # the slow webhook server only saw one POST
```

That confirms `digest.py:46-64`'s `BEGIN IMMEDIATE` held across the network
call does what the README claims for overlapping cron ticks.

Second, the per-channel atomicity and the "mark after send succeeds" ordering
are both real (`digest.py:49-64`):

```python
with db:
    db.execute("BEGIN IMMEDIATE")
    selected = store.pending_items(db, chan_cfg["name"], ...)
    if not selected:
        continue
    body = render.digest(selected, chan_cfg)
    ...
    channels.send(chan_cfg, body)
    store.record_delivery(db, chan_cfg["name"], selected)
sent += len(selected)
```

and the at-least-once tradeoff is explicitly argued in the README ("We record
delivery after `channels.send` returns successfully... Such a batch may be
sent again. Exactly-once delivery would require receiver-side idempotency").

However, there's no `try/except` around `channels.send` inside the `for
chan_cfg in cfg["channels"]` loop, so a `DeliveryError` from one channel
aborts the *entire run*, not just that channel — channels listed later in
`config.toml` are never even attempted on that tick. I reproduced this with a
permanently-broken first channel ("ops") across three consecutive simulated
cron ticks:

```
--- tick 1 ---
crashed: ops webhook permanently down
--- tick 2 ---
crashed: ops webhook permanently down
--- tick 3 ---
crashed: ops webhook permanently down
```

`energy` and `everything` — which have nothing to do with `ops`'s outage and
have their own independent pending items — never print "DELIVERED" in any of
the three ticks. The README's claim ("the failed channel remains eligible on
the next poll") is true but incomplete: it doesn't mention that every channel
*after* the failed one in config order is starved for as long as the failure
persists. In practice this is self-limiting (it clears once the offending
items age out of the live feed or the channel recovers), but it's a real,
undocumented behavior that a per-channel design should either prevent (catch,
log, continue, and raise/report once at the end) or disclose. That gap is why
this doesn't reach the 7-8 band despite the otherwise solid mechanism and
argued tradeoff.

### Existing-code respect — 5 / 6

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are
byte-identical to the original (confirmed with `diff`). The schema change is
purely additive — a new `deliveries` table via `CREATE TABLE IF NOT EXISTS`
alongside the untouched `items` table — and `--dry-run` was verified
unchanged in spirit and covered by a dedicated test
(`test_dry_run_previews_pending_without_consuming_deliveries`). The new
`test_digest.py` is a reasonable addition, not scope creep.

What costs a point: `store.record_items` (`store.py:42-51`) is still called
unconditionally on every poll for every observed item, completely unchanged
from the original:

```python
def record_items(conn, source, items):
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [...],
    )
```

I confirmed this grows without bound — four identical polls against the same
fixture produced 24 rows in `items` (6 items × 4 polls) while `deliveries`
correctly stayed at 12. The rubric calls this out explicitly ("Reusing the
`items` archive is fine *if* the unconditional insert is dealt with"). The
agent sidestepped the archive for identity purposes by building a separate
ledger, which is a defensible choice, but it never discusses or bounds the
archive's own unconditional-insert growth — the "next person" inherits an
ever-growing duplicate-laden table with no comment explaining why that's okay.

### Code quality — 3.5 / 4

Clean, readable, no dead code (the now-unused `from pathlib import Path`
import was correctly removed from `digest.py`), sensible SQL (parameterized
throughout, a composite `PRIMARY KEY (channel, source, item_key)` that makes
accidental duplicate delivery rows impossible rather than just unlikely), and
the schema migration for an existing database is genuinely handled and
tested (`test_legacy_archive_is_not_treated_as_delivery_history`). Minor
deduction: manually issuing `db.execute("BEGIN IMMEDIATE")` inside a `with
db:` block is a slightly non-obvious combination (it works, verified on
Python 3.13 and 3.14, but relies on sqlite3's implicit-transaction semantics
not being triggered first) and gets one comment, not an explanation of why
the combination is safe.

### Documentation — 3.5 / 4

The README is real prose, not a docstring, and covers exactly what the
rubric asks for: the dedupe mechanism and its boundaries ("## Delivery
identity and guarantees"), how to reset it ("Keep using the same persistent
`--db` path... deleting it or using a fresh database starts delivery over"),
and what happens on first run / with a pre-existing database ("### Existing
databases"). It also documents the concurrency story and adds a
"## Verification" section pointing at the test suite. The deduction is for
the incomplete failure-mode claim identified above — "the failed channel
remains eligible on the next poll" reads as if only that channel is
affected, when in fact every later channel in the config is also blocked
until the failure clears, and a reader relying on this README would not know
that.

## 3. What it missed

- **Fallback-chain identity.** `raw_id` is computed by `feeds.py` for two of
  the three feeds and is strictly more robust than URL canonicalization for
  those feeds (immune to any future tracking-param vocabulary, not just the
  three strings hardcoded today). The agent silently chose URL-only identity
  for every feed and never discusses why it declined the stronger signal
  where one exists.
- **Archive growth.** `items` keeps growing forever on every poll regardless
  of delivery status, with no retention policy, index on `(source, link)`,
  or even a comment flagging it as a known, accepted tradeoff.
- **Channel-failure fan-out.** No decision is recorded about whether one
  channel's outage should be allowed to block unrelated channels for the
  rest of that run. The behavior (it does block them) is a byproduct of
  preserving the original's "let it crash" philosophy inside a new loop
  shape, not a deliberate choice that's stated anywhere.
- **Webhook idempotency.** The README correctly notes receiver-side
  idempotency would be needed for exactly-once, but doesn't suggest even a
  simple mitigation (e.g., an idempotency key in the webhook payload) that
  the new `item_key` would make almost free to add.

## 4. Bugs

**Bug 1 — tracking-parameter allowlist resurrects already-delivered items.**
Reproduction above: take a newsroom item whose `entry_id` and content are
unchanged, append any query parameter not in `{utm_*, fbclid, gclid}` (e.g.
`&ref=newsletter`), and it gets redelivered even though the item's own
`raw_id` never changed. Minimal fix: for feeds with a non-null `raw_id`,
prefer `(source, raw_id)` as the key and fall back to the canonicalized URL
only when `raw_id` is `None`.

**Bug 2 — one stuck channel starves every later channel, every tick,
indefinitely.** Reproduction above: with `ops` (first in
`config.example.toml`) permanently raising `DeliveryError`, `energy` and
`everything` receive zero deliveries across three consecutive simulated cron
ticks, despite having their own unrelated pending items. Nothing in
`digest.py`'s loop catches `channels.send`'s exception per-channel; it
propagates immediately and aborts the whole `run_once`. Minimal fix: catch
`DeliveryError` per channel, log and continue to the next channel, then
raise (or exit non-zero) once at the end of the loop if anything failed.

```json
{
  "identity_strategy": 7,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 6,
  "existing_code_respect": 5,
  "code_quality": 3.5,
  "documentation": 3.5,
  "total": 33,
  "would_merge": "yes-with-fixes",
  "one_line": "Strong per-channel dedupe ledger; tracking-param allowlist and channel-ordering starvation need fixing."
}
```
