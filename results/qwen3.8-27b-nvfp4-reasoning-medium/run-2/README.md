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
| `store.py` | SQLite: archive of everything seen + per-channel "already sent" state |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

## De-duplication

Feeds keep their recent items around, so without care a 15-minute cron job
re-sends the same items every tick. Each channel only receives items it has
not already been sent, tracked in the `sent` table.

An item's identity is `(source, key)`. `key` is assigned per feed format in
`feeds.py` and is whichever field of that format is stable across fetches
and edits:

| format | key | why |
| --- | --- | --- |
| `newsroom` | `entry_id` | the URL carries rotating `utm_*` params |
| `blogroll` | `permalink` | the feed has no id of any kind |
| `generic` | `link` | the provider regenerates `guid`s whenever an item is edited |

`sent` is written only after a delivery succeeds, so a channel whose webhook
is down gets its items on the next tick, while channels that already
delivered do not re-send. `--dry-run` never writes `sent`.

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
