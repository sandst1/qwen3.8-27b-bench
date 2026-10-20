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

## De-duplication

Each run remembers which items it has already delivered to each channel, in the
`sent` table, so a channel does not get the same item on every 15-minute tick.
An item is marked delivered only *after* the channel has accepted it, so a
failed delivery is simply retried on the next tick rather than dropped.

Items are identified by `(source, link)`, not by the provider's own id: the
three providers cannot agree on one (newsroom has a stable `entry_id`, blogroll
has none, wire regenerates its `guid` on every edit), and the article URL is
the only field that stays stable across all three. See `store.dedup_key`.

A channel added later sees the current matching batch once and then only new
items from then on.

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
