# Review: notify-digest, run-4

## 1. Summary

The agent added a per-channel `deliveries` ledger keyed on a new
`feeds.item_key()` — source plus the URL stripped of query string and
fragment — and made `digest.py` skip anything already in that ledger,
recording a delivery only after a channel's `send()` call returns
successfully. It documented the three ambiguous forks (per-channel vs.
global suppression, edited-item-as-new vs. same, first-run backfill) in both
code comments and a new README section, and I verified against both fixture
snapshots that rotating `utm_campaign` params, a missing `raw_id`, and a
regenerated `guid` are all correctly absorbed without re-sending, and that a
mid-batch channel failure leaves the succeeded channel un-repeated while the
failed one retries. I would merge this: it is a well-reasoned, minimally
invasive fix, with one real gap (the `items` archive still grows an
unconditional duplicate row every single tick, which the agent never
addresses or discusses) and one documentation gap (no "how to reset" story).

## 2. Per-category scoring

### Identity strategy — 9 / 10

`feeds.py` adds:

```python
def item_key(item):
    """Stable identity of an item, used to avoid re-sending it. See README.

    Deliberately NOT raw_id:

    - blogroll has no id at all (raw_id is None),
    - the generic provider regenerates its guid whenever an item is edited,
      so an edited story would look brand new and be re-sent,
    - newsroom ids are stable, but one rule for all providers is simpler.

    The URL path is the one field every provider keeps stable. We drop the
    query string and fragment because the newsroom feed appends rotating
    campaign parameters (?utm_campaign=w34) that would otherwise make the
    same story look new every week. None of our three providers
    distinguishes items by query string; if one ever does, revisit this.
    """
    parts = urlsplit((item.get("link") or "").strip())
    path = parts.path.rstrip("/").lower()
    return f"{item.get('source', '')}|{parts.scheme.lower()}|{parts.netloc.lower()}|{path}"
```

I copied the repo to `/tmp/nd_test`, pointed `config.toml` at
`snapshot-a`, ran a real (non-dry) send, then repointed at `snapshot-b` and
ran again. Result:

```
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
  https://newsroom.example/2026/08/port-fees-union?...&utm_campaign=w34
=== everything ===
Firehose — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```

Only the genuinely-new newsroom story went out. Confirmed suppressed:

- `entry_id 84121` "Regulator opens inquiry into port fees" — same URL,
  `utm_campaign` rotated `w33→w34` — **not** re-sent.
- blogroll "Notes on port fee arithmetic **(updated)**" — title and excerpt
  edited, same `permalink` — **not** re-sent.
- wire `guid` `wire-2026-08-14-0031` → `wire-2026-08-14-0031-r2` — same
  `link` — **not** re-sent.

All three named failure modes in the prompt/fixtures (rotating
`utm_campaign`, missing `raw_id`, regenerated `guid`) are handled by one
rule, and the rule states its own blind spot ("if one [provider] ever
distinguishes items by query string, revisit this"). It's a single uniform
strategy rather than a true per-feed fallback chain (e.g. trusting
`newsroom`'s stable `entry_id` where available), which is why this isn't a
10 — but it survives every fixture case with reasoning, which is the bar for
the top band.

### Ambiguity handling — 8 / 8

All three forks named in BENCHMARK.md are explicit in both code and README:

- **Per-channel vs. global**: `store.py` docstring — "dedup is per channel,
  because two channels can legitimately want the same story" — and the
  schema backs it: `PRIMARY KEY (channel, item_key)`.
- **Edited item as new vs. same**: README — "Trade-off: an item edited
  after delivery is *not* re-sent (we key on the URL, not the content)."
  Verified above with the blogroll "(updated)" post.
- **First-run backfill**: README states the default ("the ledger starts
  empty, so every item currently in the feeds is sent once more") and gives
  a runnable backfill script. I ran it verbatim from the README against
  `/tmp/nd_backfill`:

  ```
  Running backfill script from README verbatim:
  exit: 0
  Now running digest.py for real, should send 0 items since backfilled:
  sent 0 items
  ```

  It works exactly as documented.

### Failure-mode reasoning — 8 / 8

`digest.py`'s `run_once`:

```python
channels.send(chan_cfg, body)
store.record_deliveries(db, chan_cfg["name"], [key for key, _ in selected])
sent += len(selected)
```

The delivery is only marked *after* `send()` returns without raising.
`store.record_deliveries` docstring: "Call only AFTER a successful send, so
a failed delivery is retried on the next run (at-least-once, never silently
dropped)."

I reproduced a two-channel run where the first channel (`stdout`) succeeds
and the second (`webhook` to a refused port) fails:

```
=== ops ===
Ops digest — 2 item(s)
...
channels.DeliveryError: broken: <urlopen error [Errno 61] Connection refused>
EXIT CODE: 1
```

`sqlite3 digest.sqlite3 "select * from deliveries"` after that crash shows
only `ops` rows — `broken` recorded nothing. On retry (still broken), `ops`
does **not** re-print (no duplicate stdout block), confirming per-channel
"channel two failing doesn't re-punish channel one." The at-least-once
choice is argued, not just implemented, and I could not make it lie.

### Existing-code respect — 4.5 / 6

Diff is minimal and additive — `channels.py`, `render.py`, and
`config.example.toml` are untouched; `feeds.py` gains one function and keeps
`fetch()` verbatim; `--dry-run` still works and still leaves the ledger
untouched (verified: two consecutive `--dry-run` calls both list the full
item set, since dry-run never calls `record_deliveries`).

The one real miss: the rubric flags "reusing the `items` archive is fine
*if* the unconditional insert is dealt with" — it was not. `store.py`'s
`record_items` is called unconditionally on every fetch, unchanged:

```python
def record_items(conn, source, items):
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [...for i in items],
    )
```

Since cron runs every 15 minutes and a feed item typically survives several
polls before rolling off, this table accumulates duplicate rows forever —
I confirmed it goes 6 → 12 rows across two identical back-to-back runs of
the same snapshot in `/tmp/nd_test`. The agent added a whole dedup ledger
but never asked whether the *archive* table needed the same treatment, and
the module docstring doesn't discuss it (it also silently drops the
original's "Nothing reads it at the moment" caveat, which was the only clue
that this was already a low-stakes, write-only table). Not a regression —
this behavior predates the fix — but it's exactly the omission the rubric
calls out by name.

### Code quality — 4 / 4

Clean, idiomatic, no dead code introduced, and the schema change is a
migration-safe additive `CREATE TABLE IF NOT EXISTS deliveries (...)` next
to the existing `items` table — no `ALTER TABLE` needed. I built an
old-schema DB by hand (`items` table only, one row) and ran the new
`store.connect()` against it:

```
items count: 1
tables: ['items', 'sqlite_sequence', 'deliveries']
```

No data loss, no migration script required. `INSERT OR IGNORE` on the
`(channel, item_key)` primary key is the right idempotent-insert idiom for
a ledger that might be called twice.

### Documentation — 3.5 / 4

README gets a full section, "Deduplication: why you don't get the same
items twice," covering the ledger mechanism, the `item_key` rule with a
per-provider table of *why* each provider's own id was rejected, the
edited-item trade-off, and the first-run backfill script (verified working,
above). This clears the "docstring alone caps at 2" bar by a wide margin.

Missing: no explicit "how to reset it" story. If an operator *wants* to
force a re-send (e.g. after a config change, or to recover from a
bad delivery), there is no mention of `DELETE FROM deliveries` or deleting
the db file — I checked with `grep -i "reset\|delete\|truncate"` against the
README and found nothing. The rubric asks for this explicitly ("how to
reset it, and what happens on first run") — first-run is covered well,
reset is not.

## 3. What it missed

- **`items` archive growth** (see existing-code-respect above): every tick
  re-inserts every currently-live item into `items`, unconditionally,
  forever. Not new, but also not addressed or even flagged as a known
  limitation now that the module has a dedup story for deliveries but not
  for the archive.
- **No "how to reset dedup" documentation** — an operator who needs to
  force a re-send has no guidance.
- **No discussion of channel rename/removal**: the ledger is keyed by
  `chan_cfg["name"]`; renaming a channel in config silently resets its
  dedup state (every previously-delivered item looks new again) and old
  rows for the abandoned name are orphaned forever. Plausible in practice
  (config gets tidied up occasionally) and never mentioned.
- **Content changes without a URL/path change** are, by construction,
  invisible to `item_key` — this is the stated trade-off, correctly
  documented, but worth flagging as intentional scope: a story that gets a
  significant correction (not just a title tweak) at the *same* URL will
  never be re-sent, permanently.

## 4. Bugs

None found. Everything I tried to break — snapshot-a → snapshot-b identity
survival, per-channel partial-failure/retry semantics, `--dry-run`
non-mutation, schema migration onto a pre-existing single-table database,
and the README's own backfill script — worked exactly as documented. The
only defect is the omission noted above (unbounded `items` growth), which
is silent rather than broken: the program does not crash or mis-behave, it
just keeps writing rows nobody reads.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 8,
  "existing_code_respect": 4.5,
  "code_quality": 4,
  "documentation": 3.5,
  "total": 37,
  "would_merge": "yes",
  "one_line": "Solid per-channel URL-path dedup, well argued and tested; archive growth left unaddressed."
}
```
