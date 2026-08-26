# Review: notify-digest dedup fix

## 1. Summary

The agent added a per-channel `sent` ledger keyed on the article link with
`utm_*` params stripped (`feeds.item_key`), consulted before each send and
written only after `channels.send()` returns successfully, and documented the
identity choice and its trade-off (edits are not resent) in both code and
README. I verified against both fixture snapshots: repeated runs on the same
snapshot send nothing, and moving from snapshot-a to snapshot-b sends only
the genuinely new item while correctly suppressing edited items, exactly as
documented. I would merge this with two follow-ups: the `items` archive still
grows one duplicate row per item per tick forever (unaddressed, though
harmless today), and the identity key isn't scoped by source, which is a
latent collision risk the fixtures happen not to exercise.

## 2. Per-category scoring

### Identity strategy — 9/10

The agent rejected per-feed `raw_id` entirely and settled on one rule: the
link with tracking parameters stripped, `feeds.py:87-113`:

```python
def normalize_link(link):
    """Drop tracking params (utm_* and friends) that providers rotate
    between fetches: newsroom serves the same story with a fresh
    utm_campaign every week, so the raw link is not a stable identity."""
    parts = urlsplit(link)
    if not parts.query:
        return link
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_")]
    return urlunsplit(parts[:3] + (urlencode(kept), parts.fragment))


def item_key(item):
    """Stable identity of an item across fetches, used to avoid resending.

    We key on the normalised link, NOT the provider's own id, because both
    providers that ship ids make them unreliable in opposite ways:
    newsroom's entry_id is stable but its link rotates tracking params
    (handled by normalising), while wire regenerates its guid whenever an
    item is edited (so raw_id would resend edited items). The link is the
    only identifier stable for all three feeds, including blogroll which
    ships no id at all.
    ...
    """
    return normalize_link(item["link"])
```

This is a reasoned, single rule rather than a per-feed branch, but it does
hold up against all three fixture feeds, and I verified it directly:

- **newsroom** (`raw_id` stable, link rotates `utm_campaign` w33→w34):
  re-running against snapshot-b re-sent only the brand-new entry
  (`entry_id 84130`); the two carried-over stories (`84121`, `84118`,
  same content, new `utm_campaign`) were correctly *not* resent.
- **blogroll** (`raw_id` always `None`): identity falls straight through
  to the permalink, which is present and stable — no special-case needed.
- **wire** (`raw_id`/`guid` regenerates on edit: `wire-2026-08-14-0031` →
  `wire-2026-08-14-0031-r2`): the edited item was correctly *not* resent
  under snapshot-b, because the link (`https://wire.example/i/0031`)
  didn't change even though the guid did.

Failure mode is stated explicitly, not left implicit — `feeds.py:110-112`:
`"Deliberate consequence: an edit to an item we have already delivered
(wire's "-r2" guid, a blog post retitled "(updated)") is NOT resent."`

Docked one point: `item_key` doesn't include `source`, so two different
feeds that happen to publish the same link would collide in the `sent`
ledger — see **Bugs** below. It doesn't happen with the current three feeds
(different hostnames), but it's a gap in an otherwise well-argued identity.

### Ambiguity handling — 6/8

Two of the three named forks in the rubric are surfaced and resolved
explicitly, in both code and README:

- **Per-channel vs. global suppression** — resolved per-channel, stated
  in the schema and in `digest.py:48-49`:
  ```python
  for chan_cfg in cfg["channels"]:
      seen = store.already_sent(db, chan_cfg["name"])
  ```
  and in the README: *"one row per channel + item"*. I confirmed this
  behaviourally — a single item that matches two channels' keyword filters
  appears in both digests on the first run and is suppressed independently
  per channel thereafter.

- **Edited item as new vs. same** — resolved as "same, not resent", argued
  explicitly in README.md:44-48:
  > "The normalised link is the one identity that works for all three.
  > Accepted trade-off: an edit to an already-delivered item (retitled blog
  > post, wire "-r2" guid) is *not* resent. If we ever want "notify on
  > significant updates", that's a new feature to design, not a side effect
  > of the dedup key."

- **First-run backfill** — not mentioned anywhere, in code or docs. On a
  fresh `digest.sqlite3` the `sent` table is empty, so the very first
  invocation sends everything currently in the feeds' windows as if it
  were all new. That's a defensible default, but it is an undiscussed
  fork — nothing flags that a first deploy (or a deleted DB) will flood
  every channel with the whole current backlog at once.

Two forks named and well-argued, the third decided silently (if
reasonably) — solidly in the 4-6 band, at the top of it.

### Failure-mode reasoning — 7/8

Marking is per-channel and happens only after a successful send,
`digest.py:61-62`:

```python
channels.send(chan_cfg, body)
store.mark_sent(db, chan_cfg["name"], [feeds.item_key(i) for i in selected])
```

I verified the partial-failure window directly by monkey-patching
`channels.send` to raise for the `energy` channel only, mid-run (config has
`ops`, `energy`, `everything` in that order):

```
run_once raised as expected: simulated webhook outage
sent table rows:
  {'channel': 'ops', 'item_key': 'https://blog.example/port-fee-arithmetic'}
  {'channel': 'ops', 'item_key': 'https://newsroom.example/2026/08/port-fees'}
  {'channel': 'ops', 'item_key': 'https://wire.example/i/0031'}
```

`ops` (processed before the failing channel) is marked and won't be
resent; `energy` (the one that failed) is unmarked and will retry
in full next tick; `everything` (never reached because the exception
propagated) is also unmarked and will be attempted fresh next tick. No
channel loses an item, and no channel that already succeeded gets a
duplicate — that's a correct at-least-once design, consistent with the
pre-existing `channels.py` comment ("if a channel is down we let the
exception propagate and cron will pick us up again on the next tick") that
the agent did not touch.

The tradeoff is stated, if briefly — `store.py:67-69`:
```python
def mark_sent(conn, channel, keys):
    """Record delivery only after channels.send() returned successfully,
    so a failed delivery is retried on the next cron tick."""
```
and in the README: *"Delivery state is written after a successful send, so
if a webhook is down the items are retried next tick."* This names the
guarantee (retry-on-failure, i.e. at-least-once) but never uses that term
or discusses the (very small, pre-existing) crash window between a
successful `send()` and the `mark_sent()` commit. Solid, not maximal.

### Existing-code respect — 4/6

`channels.py`, `render.py`, `config.example.toml`, and the fixtures are
untouched. `feeds.py` gained two pure functions and nothing else was
touched — the three format branches in `fetch()` are byte-for-byte
identical to the original. `--dry-run` was verified to still work and,
importantly, to not consume dedup state (confirmed: running `--dry-run`
twice in a row on the same snapshot prints the same preview both times,
and a subsequent real run still sends everything). The new `sent` table is
added via an idempotent `CREATE TABLE IF NOT EXISTS`, so an existing
production `digest.sqlite3` upgrades with no migration step.

What costs points: the rubric flags this exact codebase's known trap —
"reusing the `items` archive is fine *if* the unconditional insert is dealt
with" — and the agent left `store.record_items()` being called unconditionally
on every tick, completely unaddressed:

```python
# digest.py:42, unchanged from the original
store.record_items(db, feed_cfg["name"], items)
```

I confirmed the consequence directly: after three ticks over
snapshot-a/snapshot-a/snapshot-b, the `items` table has duplicate rows for
every item still present in the feed window on each tick (e.g. both
`blogroll` posts appear 3 times, `wire`'s two items 3 times each) — the
archive now grows by roughly (feed window size) rows every 15 minutes,
forever, and neither the code nor the updated `store.py` docstring
mentions this. It's not a correctness bug (the table was already described
as "nothing reads it," and still isn't read), but it is precisely the
condition the rubric calls out as needing to be "dealt with," and it
wasn't — not fixed, not discussed, not left as a call-out for the next
person.

### Code quality — 4/4

The diff is small and readable: two focused helper functions in
`feeds.py`, two focused helper functions plus one table in `store.py`,
and a five-line change to `digest.py`'s hot loop. No dead code, no
vestigial columns — the new `sent` table's three columns (`channel`,
`item_key`, `sent_at`) are all used, `sent_at` is defensible for future
debugging/reset tooling. SQL is sensible: `PRIMARY KEY (channel, item_key)`
plus `INSERT OR IGNORE` is the correct idiom to make `mark_sent` safely
re-callable, and I don't see anywhere it would raise on a duplicate.
Schema evolution for an existing on-disk DB is handled for free by the
additive `CREATE TABLE IF NOT EXISTS`.

### Documentation — 2.5/4

The README gained a substantial, well-written "Deduplication" section
(`README.md:27-48`) that explains the mechanism, cites all three fixture
feeds by name with the exact failure mode each one represents, and states
the edited-item trade-off explicitly — this is well above a docstring, so
it's not capped at 2. But the rubric asks for three things and only one is
covered in depth:

- dedupe behaviour: yes, thoroughly (as above).
- how to reset it: **not mentioned anywhere.** There is no note that
  deleting `digest.sqlite3` (or clearing the `sent` table) is how to
  re-notify everyone, which is exactly the kind of thing an operator will
  want mid-incident.
- what happens on first run: **not mentioned anywhere** — see Ambiguity
  handling above; a first deploy (or DB loss) silently sends the entire
  current backlog to every matching channel, and nothing warns for it.

## 3. What it missed

- **First-run backfill** is an unstated, unhandled fork. Nothing prevents
  or warns about a fresh `digest.sqlite3` (first deploy, or an operator
  deleting the DB to "reset") sending the entire current feed window to
  every channel at once.
- **No reset instructions.** There's no documented (or scripted) way to
  say "resend everything to channel X" or "resend this one item" short of
  hand-editing SQLite.
- **The `items` archive keeps growing with duplicate rows** every tick,
  unconditionally, forever — the exact condition the task's own codebase
  primes you to notice (`store.py`'s own original docstring). The agent
  neither pruned/deduped it nor called out the growth as a known,
  accepted cost.
- **`item_key` doesn't scope by source** — see Bugs.

## 4. Bugs

No outright crash or dropped-batch bug. One real, if latent, correctness
gap:

**Cross-feed key collision.** `feeds.item_key()` (`feeds.py:99-113`) hashes
only the normalised link, not `(source, link)`. If two different feeds ever
publish the same canonical link — plausible for a wire syndicate mirroring
a newsroom story, or any staging/test config that points two feed entries
at the same URL — the second one is silently and permanently treated as
"already sent" the moment the first is delivered to a channel, even though
it's a different story from a different source. Reproduction (scratch
script against the agent's own `feeds.py`, not a fixture — the three real
feeds happen not to collide since they use different hostnames):

```python
import feeds
item_a = {"title": "Newsroom original", "link": "https://example.com/story/42",
          "source": "newsroom", "raw_id": "42"}
item_b = {"title": "Wire syndication of the same story", "link": "https://example.com/story/42",
          "source": "wire", "raw_id": "wire-999"}
feeds.item_key(item_a) == feeds.item_key(item_b)  # -> True, collision
```

Output when run:
```
key(a) = https://example.com/story/42
key(b) = https://example.com/story/42
collide: True
```

Not exercised by the provided fixtures (verified: all runs against
snapshot-a/snapshot-b behaved correctly with no false suppressions), but a
real gap in the identity design that the README's confident claim — "the
one identity that works for all three" — doesn't acknowledge.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 7,
  "existing_code_respect": 4,
  "code_quality": 4,
  "documentation": 2.5,
  "total": 32.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid tested per-channel link dedup; misses first-run, reset docs, and archive growth."
}
```
