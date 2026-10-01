# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it. It does not
consult the delivery ledger, so it always shows the full current feed.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Choosing what goes in a digest

The feeds mostly repeat their current contents on every poll, so without
memory every 15-minute tick would re-send the same stories. A channel only
gets items it has **never been sent before**. Two occurrences count as the
same item when they have the same source *and* the same link with its query
string and fragment stripped — never by provider id: the wire provider
regenerates its `guid` whenever an item is edited, and newsroom appends a
rotating `utm_campaign=` to its urls, so ids and raw urls change for stories
people have already seen (that is exactly the spam this rule removes).
Matching is per channel: an item matching two channels is sent once to each,
and a brand-new channel is backfilled with everything currently in the
feeds.

History lives in the SQLite file (`--db`): `items` archives every fetch,
`delivered` is the ledger of what each channel has received. Items are added
to the ledger *after* a successful send, so if a send fails the run aborts
and the next tick retries the items that never got through. Deleting the db
file (or its `delivered` table) resets everyone's memory.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite archive of everything seen + per-channel delivery ledger |
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
