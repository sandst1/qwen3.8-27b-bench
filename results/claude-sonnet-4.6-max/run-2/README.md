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
| `store.py` | SQLite archive **and deduplication gate** |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |
| `test_dedup.py` | Tests for the deduplication logic |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deduplication

Each cron run fetches the full feed, then drops any item already recorded in
the SQLite database before sending.  The stable identity used for the check
differs by feed format — see `store.py` for the full rationale:

| Format | Stable field | Why not the other field |
| --- | --- | --- |
| `newsroom` | `raw_id` (entry\_id) | URL has rotating UTM campaign tags |
| `generic` (wire) | `link` | Provider regenerates the guid on every edit |
| `blogroll` | `link` (permalink) | No id field exists |

`store.seen_before()` uses both `link` and `raw_id` in an OR check so that
all three formats are covered by a single query.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
