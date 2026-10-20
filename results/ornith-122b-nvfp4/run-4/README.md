# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite archive of delivered items; used to dedupe |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deduplication

The feeds return the same items on every tick, so `run_once` only delivers
items it has not already delivered. Before sending, it asks the `items` table
which of this tick's items are new; after a channel successfully delivers,
those items are archived and won't be sent again.

The one design choice worth understanding is **what counts as "the same
item"**. We dedupe on the item's **link, with the query string stripped**
(`store.dedup_key`). No `raw_id` works across all three feeds:

- **newsroom** — stable `entry_id`, but its link carries a rotating
  `utm_campaign`, so the raw link changes for the same article.
- **wire** — stable link, but its `guid` is regenerated whenever the story is
  edited, so `raw_id` changes for the same article.
- **blogroll** — has no stable id at all.

The link path is the only identifier stable across all three, and stripping
the query string is what makes newsroom's rotated tracking params collapse to
the same key. If you add a feed whose links are not a reliable identity, that
is the thing to revisit.

Dedupe means "don't re-deliver what we already **sent**". Items that matched no
channel are not archived, so adding a new channel later can still surface them.

Upgrading: `store.connect` adds the `dedup_key` column to pre-existing
databases and backfills it from `link`, so the first run after deploy does not
re-deliver the existing archive.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
