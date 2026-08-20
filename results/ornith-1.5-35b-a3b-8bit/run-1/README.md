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

## De-duplication

Each run sends only items that have not been delivered before. The `items`
table records every item seen and, once an item has been delivered, when
(`sent_at`). A run selects items whose `sent_at` is still NULL, delivers them,
and marks them sent; the next run skips them. That is what stops cron from
re-sending the same items on every 15-minute tick.

An item's identity is its link with the tracking query string stripped, keyed
per source (see `store.item_key`). This is deliberate: the three feed formats
disagree on what identifies an item — newsroom's URL changes with a campaign
tag, blogroll has no id, and the wire's guid regenerates on every edit — but
they all agree on the link.

`store.migrate()` upgrades a database created before de-duplication existed;
its old rows are simply re-evaluated once.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite archive of everything seen, and what has been delivered |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
