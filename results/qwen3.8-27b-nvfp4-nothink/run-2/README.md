# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Deduplication

We run this every 15 minutes, and feeds keep their items listed for a while
after publication. Without dedup, every tick re-sent the same items.

Dedup is per-item, keyed by the item's **normalised link** (tracking params
like `utm_*` and fragments stripped, trailing slash removed, host
lowercased). We deliberately do **not** use the feed's own identifiers:
`blogroll` has none, and the `generic` feed regenerates its guid whenever an
item is edited. The canonical link is the only field that is stable across
all three feeds.

Consequences of that choice:

- An **edited** item (new guid, corrected headline) is **not** re-sent. If
  that ever becomes a problem, it is a feature to build (e.g. a per-channel
  "resend on edit" flag), not a fix to the dedup key.
- If a link **moves permanently**, the item is re-sent once. Accepted.
- A feed that goes silent and then returns items it listed before the gap
  will re-send them. Nothing to do about that without a TTL on `seen`, which
  we have not added.

State lives in the SQLite database (`--db`):

- `items` — archive of everything we have ever seen (one row per
  `(source, raw_id)`; edits appear as new rows).
- `seen` — one row per item already digested, keyed by normalised link.
  This is what the dedup decision reads.

If you delete the database, everything currently in the feeds will be
re-sent on the next run.

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
| `store.py` | SQLite: the `items` archive and the `seen` dedup state |
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
