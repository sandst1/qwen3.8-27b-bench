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

## Deduplication

Each run only sends items that have not been sent before. The SQLite archive
(`--db`) is the source of truth: rows are written only after a digest has been
delivered successfully, and the next run reads them back (`store.get_seen`) to
filter out what it has already sent. Nothing is marked as seen when a send
fails, so a failed delivery is retried on the next cron tick.

An item's identity is `(source, canonical link)`; the query string is stripped
from the link so tracking params (`utm_*`) or regenerated guids on edited items
don't make the same item look new on every fetch.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
