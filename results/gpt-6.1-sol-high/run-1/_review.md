# Review: notify-digest dedupe fix

## 1. Summary

The agent replaced the implicit "archive everything, dedupe nowhere" model
with an explicit per-channel delivery ledger (`deliveries` table, keyed by
channel + source + a utm/fragment-stripped URL), wrapped each channel's
check/send/record cycle in its own `BEGIN IMMEDIATE` transaction, and left
the pre-existing `items` archive untouched and re-documented as pure
observation history rather than a dedupe source. It left `feeds.py` and
`render.py` completely alone, kept `--dry-run` non-destructive, added a
7-case regression test suite, and wrote a long, specific README section
explaining the identity rule, the three ambiguity forks, and the
at-least-once failure-mode tradeoff. I would merge this as-is; the one real
gap is that the README never tells an operator how to force a resend
(delete rows from `deliveries`, or drop the db file), which is a cheap
follow-up, not a blocker.

## 2. Per-category scoring

### Identity strategy — 9 / 10

Single strategy, not a fallback chain, but it is correctly derived from the
three feeds' actual failure modes and I verified it against all three in
the fixtures. `store.py:54-66`:

```python
def item_key(item):
    """Ignore campaign tags and fragments, not article-identifying query args.

    IDs are absent in blogroll and unstable in wire; titles/content are mutable.
    Keep the source so unrelated providers do not suppress each other's items.
    """
    parts = urlsplit(item["link"])
    query = urlencode([
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ])
    link = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))
    return item["source"], link
```

It deliberately ignores `raw_id` entirely (never reads the `items.raw_id`
column for identity), because `raw_id` is `None` for blogroll and
regenerates on every wire edit (`feeds.py`'s own comment: "the provider
regenerates it whenever an item is edited"). Instead it keys off
`(source, normalized_link)`. I ran it against both fixture snapshots:

- newsroom `entry_id 84121`'s `utm_campaign` flips `w33 → w34` between
  snapshots — correctly suppressed (same key, utm stripped).
- wire's `guid` changes from `wire-2026-08-14-0031` to
  `wire-2026-08-14-0031-r2` on edit, with the same `link` — correctly
  suppressed.
- blogroll has no id at all and its excerpt/title change between snapshots
  — correctly suppressed because `permalink` is unchanged.
- newsroom `entry_id 84130` ("Union responds...") is a genuinely new
  article with a new URL path — correctly delivered.

Failure modes are stated, not just implied — README: "A changed article URL
(beyond the normalization above) is treated as a new item. There is no
cross-source story matching," and "Keep feed/channel names stable; renaming
them starts a new identity/history." Docked one point because it is a
single blanket rule rather than a stated fallback chain, and it has one
unstated residual risk: `urlencode(parse_qsl(...))` preserves the *original*
ordering of non-utm query parameters, so if a provider re-orders the
remaining query string between polls for the same article (not exercised in
the fixtures), the key changes and the item would be resent. Not a fixture
failure, but a real edge the README doesn't flag.

### Ambiguity handling — 8 / 8

All three canonical forks are named and resolved explicitly in README's
"Delivery decisions" section, not just implemented silently:

- **Per-channel vs. global suppression** — "Delivery history is per channel,
  not global. A new channel can receive current matching articles even if
  another channel already received them." Matches the schema:
  `PRIMARY KEY (channel, source, link_key)` in `store.py:20-26`.
- **Edited item as new vs. same** — "Titles, summaries, publication dates
  and raw IDs are not identity: corrections to an already delivered article
  do not trigger another notification." Verified above with the wire/blog
  edits.
- **First-run backfill** — "The first upgraded run therefore sends current
  matching items once, then subsequent runs suppress them. Back up the
  database before deployment; do not seed success history from
  observations alone." I reproduced this directly: seeding a legacy
  `items`-only db with one old row and running against it sent all 6
  currently-matching fixture items on the first pass and 0 on the second
  (see Bugs section for the exact repro — this isn't a bug, it's the stated
  behavior, confirmed).

### Failure-mode reasoning — 8 / 8

Marking is per channel, written strictly after `channels.send` succeeds, and
each channel's check/send/record cycle is its own transaction —
`digest.py:44-64`:

```python
    sent = 0
    for chan_cfg in cfg["channels"]:
        # Serialize check/send/record across cron processes sharing this DB.
        # Commit each channel separately: a later failure must not undo sends
        # already acknowledged by an earlier channel.
        with db:
            db.execute("BEGIN IMMEDIATE")
            selected = store.pending_items(
                db, chan_cfg["name"],
                [i for i in all_items if matches(i, chan_cfg)],
            )
            if not selected:
                continue
            body = render.digest(selected, chan_cfg)
            if dry_run:
                print(f"--- would send to {chan_cfg['name']} ---")
                print(body)
                continue
            channels.send(chan_cfg, body)
            store.record_delivery(db, chan_cfg["name"], selected)
        sent += len(selected)
```

I reproduced the partial-failure case for real (not mocked): pointed the
`energy` channel at an unreachable webhook port. Run 1 printed the `ops`
digest, then crashed with `DeliveryError` on `energy` (exit 1), never
reaching `everything`. Run 2, with `energy` still broken, did **not**
reprint `ops` — only the `energy` failure traceback again, confirming `ops`'s
delivery was durably recorded and not re-sent. Run 3, with `energy` fixed,
printed `energy` and `everything` and recorded both; run 4 sent nothing.
This is correct per-channel at-least-once behavior with no lost or
duplicated batches. The tradeoff is explicitly argued, not just implemented:
"This is at-least-once retry behavior, not exactly-once external delivery.
Recording before sending would silently lose notifications on a failed
send, so we record afterward. ... Eliminating that window requires
receiver-supported idempotency, which the current webhook interface does
not provide." That's the right call, correctly reasoned, and the remaining
double-delivery window (webhook accepted, process dies before commit) is
named rather than hidden.

### Existing-code respect — 6 / 6

`feeds.py` and `render.py` are byte-identical to the original — the agent
didn't touch the feed-normalization or rendering layers it didn't need to.
`channels.py` has a one-line docstring edit only; `send()`'s logic is
untouched. The pre-existing `items` archive's unconditional insert (every
poll, including re-seen items) is explicitly kept and explained rather than
silently left as a latent dedupe bug or ripped out — README: "The `items`
table remains an observation archive, including repeated observations;
seeing an article is not proof that any channel received it." That's the
"unconditional insert dealt with" case the rubric calls out: the agent
didn't repurpose a noisy archive as a correctness mechanism, it built a
dedicated ledger (`deliveries`) instead. `--dry-run` was kept
non-destructive and is covered by a dedicated test
(`test_dry_run_does_not_consume_pending_items`), which I also re-verified
manually (dry-run twice in a row on an unseen snapshot prints the same
preview both times, and does not block the later real send).

### Code quality — 3.5 / 4

Clean, small functions, no dead code, no vestigial columns added. The
`deliveries` schema is a sensible 4-column ledger with a correct composite
primary key (`store.py:20-26`) that also serves as the natural dedupe index
for the `SELECT 1 FROM deliveries WHERE ...` lookup in `pending_items`
(`store.py:69-88`). Migration for an existing deployment is a plain
`CREATE TABLE IF NOT EXISTS`, exercised directly: I built a db with only the
legacy `items` table (no `deliveries`), ran `digest.py` against it, and it
added `deliveries` additively and preserved the pre-existing row (`items`
count went from 1 to 7; nothing was dropped or altered). Docked half a point
because the `with db: db.execute("BEGIN IMMEDIATE")` idiom
(`digest.py:49-50`) is a slightly non-obvious combination of Python's
`sqlite3.Connection` context-manager commit/rollback semantics with a
manually-issued `BEGIN IMMEDIATE` — it works (verified by the agent's own
concurrency test and by my manual partial-failure repro above), and it's
commented, but a maintainer unfamiliar with `sqlite3`'s implicit-transaction
quirks could plausibly "simplify" it into something broken without
understanding why it was written this way.

### Documentation — 3.5 / 4

The README's "Delivery decisions" section is five paragraphs, each covering
a different concrete aspect of the new behavior (identity rule and why,
per-channel scope, transaction/locking model, failure semantics,
migration/backfill), not a vague "we added dedup" blurb. It also explains
`--dry-run`'s new non-consuming behavior and documents a test command. It
does not, however, say anywhere how to make the system *re-deliver*
something — e.g. "delete the matching rows from `deliveries`, or drop
`digest.sqlite3` to start over" — which the rubric explicitly calls out as
one of the three things a README should cover. That's a real, if minor,
gap given otherwise thorough docs.

## 3. What it missed

- **How to reset/force a resend.** Not documented anywhere — an operator
  who needs to re-notify a channel about something already in `deliveries`
  has to read `store.py`'s schema to figure out they can
  `DELETE FROM deliveries WHERE channel = ... AND ...`.
- **Non-utm query-parameter reordering.** `item_key` preserves the original
  order of surviving query parameters; a provider that reorders them
  between polls for the same article (not present in the fixtures) would
  produce a different key and cause a resend. Not discussed.
- **Table growth.** Neither `items` (unconditional insert every poll) nor
  the new `deliveries` table is ever pruned. This was already true of
  `items` before the change and the agent explicitly chose not to disturb
  it, which is defensible, but `deliveries` inherits the same unbounded
  growth and that isn't mentioned as a known limitation.
- **Cross-source/story-level dedupe.** Explicitly and correctly named as
  out of scope ("There is no cross-source story matching") rather than
  silently absent — listing this here only because it's a real limitation
  of the fixtures (the same port-fee story appears independently in all
  three feeds and is sent three times, once per feed, by design).

## 4. Bugs

None found. I exercised, against the real CLI (not just the mocked test
suite):

1. Fresh db, snapshot-a twice → 12 sent, then 0 sent.
2. Fresh db, snapshot-a then snapshot-b → 12, then 2 (only the genuinely
   new "Union responds..." article, correctly suppressing the edited
   blogroll post and the wire guid-rotation), then 0 on a third pass.
3. `--dry-run` before and after a real send — preview is stable and never
   consumes pending state; a real send after a dry-run still delivers the
   full batch.
4. Partial-channel-failure repro with a genuinely unreachable webhook URL:
   first channel's success survives a later channel's crash and is not
   resent on retry; once the broken channel is fixed, it and the remaining
   channel deliver correctly on the next tick, and a final run sends 0.
5. Legacy-schema migration: a hand-built db containing only the pre-existing
   `items` table (no `deliveries`) upgrades cleanly and preserves existing
   rows.

All of these match what the README claims, and all match the agent's own
test suite (`tests/test_digest.py`, 7 tests, all passing under
`python3 -m unittest discover -s tests -v`).

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 6,
  "code_quality": 3.5,
  "documentation": 3.5,
  "total": 38,
  "would_merge": "yes",
  "one_line": "Correct per-channel delivery ledger, URL-based identity, thorough docs and tests."
}
```
