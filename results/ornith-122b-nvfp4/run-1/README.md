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
| `store.py` | SQLite archive of everything seen; `sent` table tracks what each channel has already received |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Delivery & dedup

Each run only sends a channel items it has not already received. "Same item"
is decided by `feeds.item_key()`, which keys on `(source, canonical link)` —
the article URL with the query string stripped — because the three feeds cannot
agree on an identifier (newsroom has a stable `entry_id`, the wire regenerates
its `guid` on every edit, the blogroll has no id at all), and the URL is the
one field that is present and stable across all three.

Consequences worth knowing before you change this:

- A story is delivered to a given channel at most once, ever.
- An *edited* story (new excerpt, regenerated guid, "(updated)" title) keeps
  the same key and is therefore not re-sent.
- If an article moves to a new URL it is treated as new.

Dedup is per channel, so the same story can still reach both a themed channel
and the "everything" firehose. Delivery is recorded only after a channel's send
succeeds, so a failed delivery is retried on the next tick.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
