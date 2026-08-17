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
| `store.py` | SQLite archive of everything seen, plus per-channel sent-tracking |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deduplication

Items are sent to a channel at most once. The `sent` table in the DB tracks
this. The design choices, and why:

- **Identity is `(source, link)`, not the feed's id.** The `wire` feed
  regenerates guids whenever an item is edited, and `blogroll` has no stable
  identifier at all. The permalink is the one field stable across edits for
  all three feeds.
- **Tracking is per channel, not global.** Channel keyword filters overlap
  (an item can match both `ops` and `everything`), and such an item should
  appear in both digests.
- **Marked as sent only after a successful send.** If a channel is down the
  exception propagates (see `channels.py`) and the next cron tick retries.
- **Rows are pruned after 30 days** (`store.prune_sent`) so the table is
  bounded. Consequence: an item that remains in a feed for more than 30
  days is sent again. Accepted, because feeds only keep a recent window.
- **`--dry-run` does not mark items sent**, so a preview never consumes
  them.

If you change how items are identified (e.g. a feed drops permalinks), the
`sent` table becomes wrong; you will need to migrate it.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
