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

The cron job runs every 15 minutes, so each run must only deliver items it
hasn't delivered before. `digest.run_once` filters the fetched items against the
`items` table in the SQLite db (`store.seen_keys`), then records what it just
sent (`store.record_items`, keyed on a stable identity).

Identity is `(source, normalized_link)` — see `feeds.identity`. No single feed
field is reliable: newsroom URLs carry volatile `utm_*` params, blogroll has no
server id, and the wire feed regenerates its `guid` on every edit. The link is
the one thing stable across all three, so we key on it and strip tracking query
params first.

Items are recorded only *after* a successful send, so a failed delivery is
retried on the next tick rather than lost. The db schema adds a `key` column and
a unique index on `(source, key)`; a fresh db is fine, but an existing one needs
dropping or migrating to pick up the new column.

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
