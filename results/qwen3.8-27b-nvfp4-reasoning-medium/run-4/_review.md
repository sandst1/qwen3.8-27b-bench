# Review — notify-digest dedup fix

## 1. Summary

The agent added a per-channel `sent` log keyed on `(channel, source, normalised link)`, changed the identity of an article from the feed's own id to its URL with query string and fragment stripped, and moved the "mark as delivered" write to *after* a successful `channels.send()` so a failed channel is retried next tick without re-sending to channels that already succeeded. It also fixed the pre-existing unconditional insert into the `items` archive table so re-polled items aren't archived twice, and wrote a substantial README section and store.py module docstring explaining the identity choice, its caveat, the at-least-once tradeoff, and how to reset. I would merge this with minor doc additions — it is correct against both fixture snapshots, doesn't touch `feeds.py` or break `--dry-run`, but it never names the first-run backfill decision (a config change or fresh deploy will flush every currently-live item to every matching channel in one shot) and doesn't explicitly connect the new per-channel accounting to the pre-existing exception-propagation behavior in `channels.py`.

## 2. Per-category scoring

### Identity strategy — 9/10

`store.py` picks a single normalised-URL identity and states explicitly, in the module docstring, why each feed's own id fails:

```python
def normalise_link(url):
    """Return `url` with its query string and fragment removed.

    This is the stable identity of an article across all three feeds; see the
    module docstring for why the feed-native ids are not usable.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
```

and the docstring justification (store.py:16-31) walks through `newsroom` (utm rotation), `blogroll` (no id at all), `generic` (guid regenerated on edit) individually before landing on link-as-identity, plus a stated caveat about query-string-only differences. I reproduced all three cases against the fixtures:

```
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a
sent 12 items
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-a again
sent 0 items
$ sed -i 's/snapshot-a/snapshot-b/' config.toml
$ python3 digest.py --config config.toml --db digest.sqlite3   # snapshot-b
sent 2 items      # only the genuinely new "Union responds..." article
```

Between snapshot-a and snapshot-b, `newsroom`'s `utm_campaign` rotates from `w33` to `w34` on the *same* two articles, `wire`'s guid gets a `-r2` suffix on an edited item, and `blogroll`'s title/excerpt changes on an edit — none of those false-triggered a re-send. It doesn't score a full 10 because it isn't a fallback chain (it never prefers a feed's stable id when one genuinely exists, e.g. `newsroom`'s `entry_id` never changes and could have been used directly); it's one uniform rule that happens to work for all three feeds in the fixtures.

### Ambiguity handling — 5/8

Only one of the three classic forks is named and resolved in prose. Per-channel vs. global suppression is explicit, in both the README and the schema comment:

```python
CREATE TABLE IF NOT EXISTS sent (
    channel   TEXT NOT NULL,
    source    TEXT NOT NULL,
    link      TEXT NOT NULL,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, link)
);
```
> "A healthy channel is never re-sent an article just because a sibling channel failed." (store.py:16)

The "edited item as new vs. same" fork is never named as a decision, even though the identity choice *is* that decision (link-only identity means an edited article is always treated as the same item, never re-notified) — it's discussed only as a false-positive caveat about query strings, not as "we choose not to re-notify on edits." The "first-run backfill" fork is not mentioned anywhere in code or docs; nothing flags that a fresh `digest.sqlite3` (or a channel keyword change) will flush every item currently in the feed to every matching channel on the very next run. Both of these are decided silently, and in the fresh-deploy/config-change case, plausibly the wrong default for a notification system (a sudden burst rather than "nothing until something changes").

### Failure-mode reasoning — 7/8

Per-channel marking, correct ordering, and the tradeoff is argued in both places that matter:

```python
channels.send(chan_cfg, body)
# Log only after the send succeeds so a failed send is retried next tick.
store.mark_sent(db, chan_cfg["name"], fresh)
sent += len(fresh)
```

I simulated a mid-run failure on the second channel to confirm partial-failure semantics:

```
calls attempted: ['ops', 'energy']
sent rows: [{'channel': 'ops', ...}]   # only ops got marked; energy's send raised and was not marked
```

`ops` (channel 1, succeeded) is durably marked and will not be resent; `energy` (channel 2, raised `DeliveryError`) is correctly *not* marked and will be retried next tick. That's the top-band behavior. It loses half a point because the module docstring and README argue the at-least-once tradeoff only at the single-channel level, not the cross-channel one: `channels.send`'s exception aborts the `for chan_cfg in cfg["channels"]` loop entirely (unchanged, pre-existing behavior in `digest.py`/`channels.py`), so a failure on channel 2 also means channel 3 is silently skipped for that tick — delayed, not lost, but never called out as part of the new argument even though the new sent-table design makes this interaction worth stating.

### Existing-code respect — 6/6

`feeds.py`, `channels.py`, `render.py`, and `config.example.toml` are untouched (`diff -rq` confirms). `--dry-run` was verified still to work and, correctly, still not write to `sent`:

```
$ python3 digest.py --config config.toml --db digest.sqlite3 --dry-run
... prints digests ...
sent 0 items
$ sqlite3 digest.sqlite3 "select count(*) from sent;"
0
```

The unconditional insert into `items` (flagged explicitly in the rubric as something that must be dealt with if the archive is reused) is handled:

```python
def record_items(conn, source, items):
    ...
    seen = {
        normalise_link(row["link"])
        for row in conn.execute("SELECT link FROM items WHERE source = ?", (source,))
    }
    fresh = [i for i in items if normalise_link(i["link"]) not in seen]
    if not fresh:
        return
    conn.executemany(...)
```

No scope creep — no new CLI flags, no scheduler, no plugin system, exactly the two tables and one call-site change the fix needed.

### Code quality — 4/4

Clean, uses `CREATE TABLE IF NOT EXISTS` for the new `sent` table so an existing `digest.sqlite3` upgrades in place with no migration script needed. No vestigial columns — `raw_id` remains in `items` for archival lookup purposes (unrelated to the new identity scheme) and continues to be populated as before. SQL is straightforward (`INSERT OR IGNORE`, simple `SELECT`s), functions are small and each has a docstring stating what it does and why.

### Documentation — 3/4

The README gained a full "De-duplication" section (README.md:31-54) covering the identity choice with its caveat, the at-least-once semantics and cross-channel independence, and how to reset (`delete the sent table`). It does not mention what happens on the very first run against a fresh database (every currently-live item goes out at once) — the one item the rubric explicitly calls for that's missing. Caps it just under full marks rather than at the docstring-only ceiling of 2, since this is genuine README prose, not just a docstring.

## 3. What it missed

- **First-run / cold-start burst**: never named or discussed. On a brand-new `digest.sqlite3`, or after adding a channel, every item currently live in the three feeds is sent to every matching channel in one digest — potentially a large one-off blast. Silent, unaddressed.
- **Edited-item semantics as an explicit decision**: the identity scheme quietly decides "an edited article is the same article, never re-notified," but this is framed only as an incidental caveat about query strings, not stated as the deliberate policy it actually is (confirmed by the `wire`/`blogroll` snapshot-b edits above, neither of which re-triggered a send).
- **Cross-channel abort on a mid-loop crash**: unchanged from the original code, but the new sent-table design interacts with it (a channel a few positions later in `cfg["channels"]` simply never runs this tick if an earlier one raises) and this interaction is never called out alongside the at-least-once argument.
- **No `UNIQUE` constraint on `items(source, link)`**: dedup on insert is enforced only in Python via a `SELECT` + set membership check rather than at the schema level, so concurrent runs (not a real risk given cron's serial nature, but worth a one-line note) could still race.

## 4. Bugs

None found. I reproduced:
- clean run on snapshot-a → 12 items sent, then the identical run again → 0 items (dedup holds within a snapshot);
- snapshot-a → snapshot-b transition → only the genuinely new article sent, despite `utm_campaign` rotation, a regenerated `guid`, and two edited items;
- `--dry-run` leaves `sent` untouched and is idempotent across repeated dry runs;
- a simulated failure on the second of three channels correctly withholds `mark_sent` for that channel while the first channel's marks persist.

```json
{
  "identity_strategy": 9,
  "ambiguity_handling": 5,
  "failure_mode_reasoning": 7,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 3,
  "total": 34,
  "would_merge": "yes-with-fixes",
  "one_line": "Solid per-channel dedup by normalised link; misses first-run backfill and edit-as-same framing."
}
```
