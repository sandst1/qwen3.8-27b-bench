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
| `store.py` | SQLite archive of everything seen + dedup ledger of what was sent |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Dedup

Feeds re-serve items on every fetch, so each item is sent to each channel at
most once. `digest.sqlite3` records what has been delivered where (the
`sent` table in `store.py`), keyed on `(feed name, stable item id)`.

The stable id is the `key` field that `feeds.py` adds to every item, and it
is chosen per feed format because none of the providers offers one that is
universally reliable:

- `newsroom`: `entry_id`. The feed URLs carry rotating `utm_campaign`
  tracking params, so the URL must not be the identity.
- `blogroll`: the permalink. This feed has no other identifier, and titles
  and excerpts change when posts are edited; permalinks do not.
- `generic` (wire): the link. The provider regenerates the `guid` whenever
  an item is edited, so edited items are still recognised as the same item.

Dedup is per channel, not global. Markers expire after
`SENT_RETENTION_DAYS` (30) in `store.py`; an item that reappears after that
is treated as new and sent again.

Related semantics:

- Items are marked **after** a successful send. If a channel is down the
  exception propagates and the next cron tick retries it, without re-sending
  to channels that already got the items.
- `--dry-run` prints what would be sent and marks nothing.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
