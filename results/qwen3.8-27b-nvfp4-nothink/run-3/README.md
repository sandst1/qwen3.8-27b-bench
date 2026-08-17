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

## Deduplication

Each item is sent at most once. The SQLite `items` table (see `store.py`)
records every link we have ever seen; a run only sends links it has not
seen before. Items are identified by their normalised link, not by the
provider's id: the blogroll feed has no id, the wire feed regenerates its
guid on every edit, and the newsroom feed rotates a `utm_campaign` URL
parameter between polls (stripped by `feeds.normalise_link`).

Consequences to be aware of:

- If a feed relinks an item (new URL, same story), it will be sent again.
- If you delete `digest.sqlite3`, everything currently in the feeds will
  be re-sent on the next run.
- `--dry-run` records items as seen, so it "uses up" one-run's worth of
  new items.

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
