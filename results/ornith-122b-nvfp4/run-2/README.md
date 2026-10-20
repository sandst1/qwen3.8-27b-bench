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
| `store.py` | SQLite archive (`items`) plus the per-channel dedup ledger (`deliveries`) |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deduplication

Each cron tick only sends items a channel has not received before. We keep a
`deliveries(channel, key)` ledger and skip anything already in it for that
channel. Delivery is per-channel: the same story can land in several channels
in one run, and each gets it once.

The hard part is the `key` — the three feeds have no common stable id, so it
is chosen per format in `feeds.py`:

| Feed | Stable key | Why not something else |
| --- | --- | --- |
| newsroom | `entry_id` | the `url` changes on every edit (utm params) |
| blogroll | `link` (permalink) | no provider id at all; titles/excerpts are edited in place |
| wire | `link` | the `guid` (`raw_id`) is regenerated on every edit |

Because of the wire feed, "just dedup on `raw_id`" is wrong: an edited wire
item arrives with a brand-new guid and would be resent every time it was
tweaked. Keying the blogroll on `raw_id` is also wrong — it is always `None`.
If you add a feed, set its `key` the same way.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
