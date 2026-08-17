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
| `store.py` | SQLite persistence: archive + dedup ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |
| `tests/test_dedup.py` | Unit + integration tests for deduplication |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deduplication

Each item is identified by a **canonical key**: `source + link-without-query-string`.

The query string is stripped because our feeds have two known instabilities:
- `newsroom` rotates `?utm_campaign=wNN` in every link each week.
- `wire/generic` regenerates its `guid` on every edit while keeping the `link`
  constant.

The full original link (including UTM tags) is still stored in the `items`
archive table and shown in rendered digests.  Only the internal `seen` table
uses the stripped canonical key.

The `seen` table is updated after all channels have been dispatched
successfully.  If any delivery raises, nothing is marked seen and the next
cron tick retries the full set of new items.

## Tests

```sh
python -m pytest tests/ -v
```

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
