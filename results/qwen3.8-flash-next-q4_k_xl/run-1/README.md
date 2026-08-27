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
| `identity.py` | Stable dedup key for an item (see below) |
| `store.py` | SQLite: archive of everything seen + per-channel send ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

## Deduplication

Feeds keep repeating their recent items, so every run used to re-send the
same things. Each item now gets a stable **dedup key** and a channel only
receives an item once, ever.

Why the key is the *normalised link* (tracking params like `utm_*` and the
fragment stripped) rather than an id — the three providers each break a
different "obvious" choice:

* **newsroom**: stable `entry_id`, but its feed URLs carry a `utm_campaign`
  that rotates weekly, so the raw link changes for the same story.
* **wire**: stable link, but regenerates its `guid` whenever an item is
  edited (`...-0031` → `...-0031-r2`), so keying on ids re-sends edits.
* **blogroll**: no id at all; the permalink is all there is, and it is
  stable.

Consequences, deliberately chosen:

* An edited story is **not** re-sent (duplicates were the complaint; if we
  ever want "updated" notices, that is a separate feature).
* If a provider changes a story's URL, it counts as new and is sent once.
* The ledger is **per channel**, because channels filter differently — an
  item is new *for a channel* until that channel has received it.
* Sends are recorded **after** a successful delivery, so a failed webhook
  run is retried on the next tick.

Old databases are upgraded automatically on first run: keys are backfilled
onto archived items, and because the old code had already sent everything
in the archive to every channel, the ledger is seeded from the archive so
the upgrade itself does not re-blast anything.

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
