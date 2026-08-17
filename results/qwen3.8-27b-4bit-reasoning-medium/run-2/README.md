# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it. It records
nothing, so it is safe to run as often as you like.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Duplicates: how they are prevented

Each item is delivered to a given channel **at most once**. The `sent` table
in the SQLite db remembers every (channel, item) pair that has been
delivered. Each run only sends items a channel has not already received, and
marks them as sent *after* the send succeeds, so a down channel is retried on
the next tick instead of dropping the items. The `sent` table is never
pruned — deleting a row would re-send that item to that channel.

Items are identified by a **canonical link** (the URL with its query string
and fragment stripped, scheme and host lower-cased), *not* by the feed's own
id. That is the one field that stays stable for the same story across all
three providers:

| feed       | why its own id does not work                       |
| ---------- | -------------------------------------------------- |
| `newsroom` | rewrites the `utm_*` query params on every snapshot |
| `blogroll` | has no identifier at all                            |
| `wire`     | regenerates its `guid` whenever an item is edited   |

So an edited story (new title, new guid, new utm params) is still recognised
as one we already sent and is not re-sent. The key is computed by
`store.item_key()` / `store.canonical_link()`.

The `items` table is a separate archive of every distinct item ever seen
(one row each, `first_seen` preserved). It answers "did this ever come
through?" and does not drive dedupe.

Databases created before this dedupe existed are upgraded in place on the
next run (`store._migrate()`): the old archive held one row per poll and is
collapsed to one row per item. Because the new `sent` table starts empty,
the first run after an upgrade re-sends whatever the feeds currently list,
once.

Note: this assumes one cron instance at a time (a run takes seconds; cron
does not overlap them). Two concurrent runs could both decide an item is new
and both send it.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop, per-channel filtering + dedupe |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite: item archive, `sent` dedupe table, item identity, db migration |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`. A good end-to-end test of the dedupe: run once
against `snapshot-a` with a fresh db, then again against `snapshot-b` with
the *same* db — only the genuinely new story ("Union responds to port fee
inquiry") should be sent on the second run. The updated blog post, the
re-guided wire item, and the newsroom stories with new utm params must all be
skipped.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
