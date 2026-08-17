# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it. `--all`
bypasses deduplication and re-sends everything (the sent table is still
updated, so it does not suppress future runs).

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Deduplication

cron runs every 15 minutes, and feeds keep serving items that were
published hours ago (they also re-serve edited items). To stop people
getting the same items over and over, each item is delivered at most
once: items that have already been sent to any channel are skipped.

- The `sent` table in the SQLite DB holds (source, key) for every
  delivered item.
- `key` is the feed's raw id when the feed provides one, else the item
  link. The link is the only identifier stable across all three feed
  formats (see `feeds.py` for the per-format quirks). Keying on the link
  rather than the raw id is deliberate: a provider changing its id
  scheme is far less likely than a link changing, and the failure mode
  we want is "re-send an edited item", not "silently drop it".
- An item is marked sent only *after* the channel accepts the digest,
  so a failed delivery is retried on the next tick.
- `--all` ignores the sent table for one run.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite: archive of everything seen + the dedup `sent` table |
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
