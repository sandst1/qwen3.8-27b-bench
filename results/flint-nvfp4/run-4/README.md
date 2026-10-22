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

## How repeats are avoided

Every run only sends items the archive (`digest.sqlite3`) has not seen before;
items that were already sent are filtered out before any channel is messaged.

An item is identified by its feed source plus a stable per-item id (the
provider's entry id, or the item's canonical link when the feed has none), so
the same item is delivered once — on the cron tick after it first shows up.

Delivery stays best effort: if sending raises, the error propagates and the item
is not marked as sent, so the next tick retries it. Only items that went out are
marked as seen.

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

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
