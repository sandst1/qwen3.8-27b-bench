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
| `store.py` | SQLite: observation archive + delivery ledger (`deliveries`) |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## How sending works

Each run fetches the current state of every feed and then picks, per channel,
the items that are still *pending* — that is, items that have not been delivered
successfully to that channel yet. Pending items are looked up in the `deliveries`
ledger (one row per successful send, per channel, keyed by source + link) and
anything already in it is skipped. Items are marked delivered only after the
channel accepted the digest, so a failed send is retried on the next tick.

Because the ledger is keyed per channel, a channel whose filters change picks up
exactly the items it has not received before — old items it already got are not
re-sent.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
