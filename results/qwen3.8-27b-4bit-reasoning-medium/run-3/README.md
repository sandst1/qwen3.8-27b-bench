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
| `store.py` | SQLite: dedup memory + archive of everything seen |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## How dedup works

The job runs every 15 minutes from cron, but feeds keep their recent items
around for a while, so a naive "send everything I just fetched" re-sends the
same items on every tick. To stop that, `digest.py` only sends items it has
not seen before.

"Seen before" is tracked in the `items` table in the SQLite DB, keyed by
`(source, link_key)`. On each run, for every feed we keep only the items whose
key is not already in the table, record those, and send them.

### Why `link_key` and not the feed's own id?

The three providers do not agree on identifiers:

| Feed     | Own id     | Problem                                                        |
| -------- | ---------- | -------------------------------------------------------------- |
| newsroom | `entry_id` | stable, but the URL carries rotating `utm_campaign` tracking params that change each poll |
| blogroll | *(none)*   | no identifier at all; only the permalink is stable             |
| wire     | `guid`     | regenerated whenever an item is edited (a typo fix or retile changes it) |

The one field that is stable across all three is the item's **path**. So
`link_key` is the canonical link — scheme + host + path, with the query string
and fragment dropped. Dropping the query is what keeps newsroom's rotating
`utm_campaign` from making every item look new.

The trade-off: if a provider ever used query params as part of an item's
identity, we would treat two URL variants as one item. None of the current
three do, and the path is the more durable key anyway.

### First run after a deploy

The `items` table already holds everything we have ever fetched, so after
deploying this change the first run only sends items that are genuinely new —
it does not re-send the whole current feed. Old databases are upgraded
automatically on connect: the `link_key` column is added and backfilled, and
the duplicate rows left behind by the old archive-everything behaviour are
collapsed to one row per item (earliest `first_seen` kept).

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
