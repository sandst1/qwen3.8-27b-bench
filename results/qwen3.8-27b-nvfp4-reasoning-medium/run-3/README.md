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
| `store.py` | SQLite: archive of everything seen, plus delivery state |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deduplication

Each item is delivered to each channel **at most once**. State lives in
the `sent` table in the SQLite database, keyed on `(channel, source,
item key)`. An item that matches several channels is sent to each of
them, once each.

The item key is the item's stable identity within its source, and it is
chosen per feed format in `feeds.py` — deliberately *not* the guid or
the raw URL, because the providers change those under us:

| Format | Key | Why |
| --- | --- | --- |
| newsroom | `entry_id` | URLs carry rotating tracking parameters |
| blogroll | permalink | no identifier of any other kind exists |
| generic (wire) | link | the guid is regenerated on every edit |

Consequences to be aware of:

- **First run after deploying** this sends the current feed contents as
  a baseline (nothing is in `sent` yet); from then on only new items
  are sent.
- **Failed deliveries are not marked sent**, so the next tick retries
  them. Delivery is at-least-once, not exactly-once.
- **After downtime** the next run delivers the backlog of new items in
  one digest. This is deliberate: catch up rather than silently drop.
- `--dry-run` previews exactly what a real run would send, without
  recording anything as sent.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
