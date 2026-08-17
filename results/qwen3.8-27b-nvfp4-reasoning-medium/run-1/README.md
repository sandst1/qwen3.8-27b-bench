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
| `store.py` | SQLite archive of everything seen |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deduplication

Feeds keep recent items around and cron runs every 15 minutes, so without
dedup every channel gets the same items on every run.

An item is considered the same across pollings if it has the same
`(source, raw_id)` **or** the same `(source, link)`. We match on both
because no single field is stable:

| feed     | raw_id                      | link                          |
| -------- | --------------------------- | ----------------------------- |
| newsroom | stable (`entry_id`)         | rewritten with new utm params |
| wire     | regenerated on every edit   | stable                        |
| blogroll | absent                      | stable (permalink)            |

So an edited wire item (new guid, same link) is not resent, and a
newsroom item whose URL gained utm params (same `entry_id`) is not
either. If a feed ever changes an item's link *and* its id, that item
will be sent once more — accepted trade-off, since link+id is the most
stable combination the providers offer.

Delivery is tracked per channel in the `sent` table, on purpose:

- if a channel's keyword list changes or a new channel is added, its next
  run still gets the items that match it;
- a failed delivery is not marked sent, so the next tick retries it.

`--dry-run` filters against `sent` (so it shows what would actually go
out) but writes nothing to it.

The `items` archive is deduplicated with the same rule, so `first_seen`
means what it says and the table does not grow 96× per day. Existing
databases pick this up on the next run (the `sent` table is created
automatically); pre-existing duplicate rows in `items` are left alone.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
