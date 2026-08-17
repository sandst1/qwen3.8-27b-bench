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
| `store.py` | SQLite: the `items` archive and the `sent` de-dup log |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## De-duplication

The feeds re-serve recent items on every poll, so a naive run would notify
people about the same articles on every 15-minute tick. To stop that, each
channel only receives an article **once**:

- `store.sent` records, per channel, every article already delivered. On each
  run we drop anything a channel has already been sent before rendering.
- An article is identified by its **URL with the query string and fragment
  stripped** (`store.normalise_link`), not by the feed's own id. The three
  feeds have no common stable id: `newsroom` rewrites `utm_*` query params on
  the same article's URL, `blogroll` has no id at all, and `generic`
  regenerates its `guid` whenever an item is edited. The URL path is the one
  thing stable in all three. (Caveat: two articles differing only by query
  string would be treated as one — not a problem for these permalinks.)
- Delivery is **at-least-once**. An article is logged as sent only *after* the
  send succeeds, so a failed send is simply retried on the next tick. The
  trade-off: if one webhook channel is down, the *other* healthy channels keep
  getting only genuinely new items — they are not re-sent just because a
  sibling failed.

To force a full re-notify (after a long outage, or following a config change),
delete the `sent` table; the next run starts fresh.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
