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
| `store.py` | SQLite archive of everything seen, and delivery/dedup tracking |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

## Duplicate delivery

Because this runs every 15 minutes and a channel's item list is just
"everything currently matching its keywords", each run tracks which items it
has already delivered *per channel* (`store.deliveries`) and skips them on
later runs — otherwise every run would re-notify about every still-matching
item forever. `--dry-run` does not record anything as delivered, so it's
safe to use for previewing without affecting real delivery state.

Items are matched across runs using `feeds.dedup_key()`, not just `link` or
`raw_id`, because the three providers aren't stable in the same field (see
the docstrings in `feeds.py` and `store.py` for the specifics and the
fixture evidence).


The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
