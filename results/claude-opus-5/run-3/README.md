# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest of the items it has not already seen.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it, and does not
record anything as delivered.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

```sh
python3 test_digest.py
```

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching, normalising the three feed formats, and `identity()` |
| `store.py` | SQLite archive + the delivery ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |
| `test_digest.py` | Regression tests, mostly about dedup |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deduplication

This is the whole point of the job, and it used to be broken: nothing recorded
what had been delivered, so every 15-minute cron tick re-sent the entire feed
window.

**Two items are "the same" if they have the same normalised link.** Normalising
means: lowercase scheme and host, drop the fragment, drop trailing slashes, and
strip tracking query parameters (`utm_*`, `gclid`, `fbclid`, ...). The
implementation and full reasoning are in `feeds.identity()`.

Why not something more obvious? Each of our three feeds breaks a different
obvious choice, which is exactly why the two fixture snapshots are worth
keeping:

| Candidate key | Breaks on |
| --- | --- |
| provider `guid` / `raw_id` | `blogroll` has no id at all; `wire` regenerates its guid when an item is edited (`wire-…-0031` → `wire-…-0031-r2`) |
| title or content hash | `blogroll` retitles in place (`Notes on port fee arithmetic` → `… (updated)`) and rewrites excerpts |
| raw link | `newsroom` stamps a fresh `utm_campaign` on every url each week (`w33` → `w34`) |
| **normalised link** | nothing we currently see |

Between `snapshot-a` and `snapshot-b` every feed churns, but only one item is
genuinely new (`newsroom` entry 84130). A correct run over b-after-a delivers
that one item and nothing else; `test_churned_feed_sends_only_genuinely_new_item`
pins this.

The known tradeoff: if a provider ever publishes two genuinely different items
at one url, we send only the first. That has not happened, and it fails quiet
rather than spammy. If it does, give that feed its own rule in `identity()`
rather than reverting everyone to `guid`.

### The ledger is per channel

`sent` is keyed on `(channel, identity)`, not on `identity` alone. Channels
overlap deliberately — in the sample config both `ops` and `everything` match
the port-fee story — so a global "have we sent this?" would let whichever
channel ran first swallow the item and starve the rest.

### Ordering: send first, record second

`run_once` marks items as delivered only *after* the channel accepts them. The
reverse order would turn any webhook outage into permanently dropped items. The
consequence is that a crash between delivery and commit re-sends that batch
once. That is the deliberate direction to err in: a missed item is worse than a
repeated one.

A channel that fails is logged, left unmarked so the next tick retries it, and
skipped so the other channels still get their digests.

## Operational notes

- **Upgrading an existing database is automatic.** `store.connect()` backfills
  the new `identity` column, collapses the duplicate archive rows the old code
  accumulated (keeping the earliest `first_seen`), and adds the unique index.
  It is idempotent and safe to run repeatedly.
- **First run on an empty database sends everything currently in the feeds.**
  With a fresh db and a wide window that can be a large first digest. If that
  matters for a new channel, seed the ledger before enabling delivery.
- **The `sent` table grows without bound.** It is a few rows per item per
  channel, so it is not a concern at this volume. Do not add pruning based on
  age without checking the feed window first: deleting ledger rows for items
  still present in a feed makes them get re-sent.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
