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

## How it avoids re-sending the same items

Feeds keep re-serving old items, and we run from cron every 15 minutes, so a
naive run would blast the same digest at every channel on every tick. To stop
that, the SQLite db (`--db`) remembers which items each channel has already
been sent, and a run only delivers what is **new to each channel**.

Dedup is per channel, not global, because each channel has its own keyword
filter: the same item can be old to one channel and new to another.

An item is identified by **(source feed, link with tracking params stripped)**.
This is deliberate — none of the feeds' own identifiers are reliable: newsroom
has a stable id but a URL that changes every week (`utm_campaign`), blogroll has
no id at all, and wire regenerates its `guid` whenever an item is edited. The
canonical link is the one field that is stable across all three. See the
docstring in `store.py` for the full reasoning and the one trade-off (if a
provider moves an article to a genuinely new URL, it counts as new).

**Operational note:** the db is the job's memory. Keep it in place and don't
delete it or point cron at a fresh file, or it will resend everything. Delivery
is at-least-once: a crash between sending and recording means an item may be
sent twice, which we consider acceptable for a digest.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite: archive of everything seen + which items each channel has been sent (the dedup) |
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
