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
| `store.py` | SQLite: observation archive + per-channel dedup record |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deduplication

Feeds keep returning recent items, so every run only sends items a channel
has not already received. The record is the `sent` table in the SQLite DB,
keyed per channel: an item that matches several channels reaches each of
them exactly once.

The identity of an item is `(source, link)` with the query string and
fragment stripped off the link — see `store.item_key()`. This is a
deliberate choice, because none of the per-feed identifiers are reliable on
their own:

| Feed | Problem with its own identifier |
| --- | --- |
| `newsroom` | `entry_id` is stable, but the URL gains rotating `utm_*` tracking params each week, so the raw link changes |
| `blogroll` | has no identifier at all; the permalink is the only stable field |
| `wire` | the `guid` is regenerated whenever an item is edited, so keying on it would re-send edited items |

The stripped permalink is stable for all three, and does not change when an
item is edited in place.

If a delivery fails, the items are not marked sent and are retried on the
next tick (consistent with the best-effort delivery in `channels.py`).

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
