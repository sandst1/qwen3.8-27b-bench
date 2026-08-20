# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it. It does not mark
items delivered, so a dry run never eats the backlog.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## De-duplication

The job runs on a fixed schedule and re-fetches every feed every tick, so
without help each run would re-send the whole backlog. To avoid that, `store`
keeps a `delivered` table: before sending a channel we look up which items it
has already received and skip them, then record what we just sent.

It is keyed **per channel** and **per item identity**:

* **Per channel** — a channel that was down for a while gets its backlog when
  it returns, and one channel never starves another of an item it also matches.
* **Per item identity** — an item is identified by `(source, key)`, where `key`
  is a stable value each feed provider actually guarantees. Identifiers are not
  reliable across providers, so the key is chosen per format (see `feeds.py`):

  | format    | key              | why not the obvious field            |
  | --------- | ---------------- | ------------------------------------ |
  | `newsroom`| `entry_id`       | URL carries volatile `utm` params    |
  | `blogroll`| `permalink`      | it has no id at all                  |
  | `generic` | `link`           | `guid` is regenerated on every edit  |

Using `raw_id` or the URL directly would re-send edited items, which is the
original bug.

> Upgrading: the `delivered` table is created empty on first run, so the first
> run after upgrading will re-send the current backlog once — expected.

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

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
