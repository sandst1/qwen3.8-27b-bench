# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it.

## No repeats

Feeds re-serve their rolling window on every fetch, so "just fetched" is
not "new". Each run sends each channel only the items that channel has not
already received, tracked in a SQLite ledger (`sent` table). A digest with
nothing new for a channel sends nothing.

Two things to know before changing the dedupe key (`feeds.item_key`):

- Do not key on the provider's `guid`/`entry_id` or on the full URL. The
  wire feed regenerates the guid whenever a story is edited, and the
  newsroom rotates `utm_*` params in its URLs between fetches — both make
  an already-seen story look new on every fetch. The key uses
  source + host + URL path (query stripped).
- An edited re-publication at the same URL is deliberately *not* resent.
  Distinct feeds covering one event (two outlets, two URLs) are distinct
  items and both flow.

Delivery is best-effort: if a webhook is down, nothing is marked as sent
in that run and the next tick retries the batch — including items an
earlier channel already received, which is accepted so that one dead
channel cannot silently swallow items from the others.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching, normalising the three feed formats, item identity (`item_key`) |
| `store.py` | SQLite ledger of what each channel already received |
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
