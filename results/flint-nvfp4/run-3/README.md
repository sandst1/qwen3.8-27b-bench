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
| `store.py` | SQLite archive of everything seen |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## How new items are decided

Each run fetches the configured feeds and asks the SQLite archive
(`digest.sqlite3`) which items it has not seen yet. Only unseen items are
selected, rendered, and sent; anything already in the archive is skipped. The
archive is the source of truth for "has this already gone out?", which is what
keeps the 15-minute cron from re-sending the same items forever.

An item's identity is its `raw_id` (entry id, permalink, or canonical link,
depending on feed format). A run marks an item as seen only after its delivery
succeeded, so a failed channel does not cause the item to be silently dropped.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
