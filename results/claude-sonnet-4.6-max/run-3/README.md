# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it (nothing is
delivered and the database is still updated, so items seen in a dry run will
not appear in the next real run).

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite persistence — deduplication store and archive |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |
| `tests.py` | Unit and integration tests (`python3 tests.py`) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deduplication

Each cron tick, `digest.py` calls `store.seen_links()` to load the set of
canonical URLs already stored in `digest.sqlite3`.  Only items whose link is
**not** in that set are recorded and sent; everything else is silently skipped.

**Deduplication key: canonical link (UTM-stripped URL)**

All three feed providers have quirks that make naive deduplication unreliable:

| Provider | Quirk | Solution |
| --- | --- | --- |
| newsroom | Rotates `utm_campaign` every week (`w33` → `w34` …) so the same article gets a new URL every poll | `feeds._canonical_link()` strips all `utm_*` query params |
| blogroll | No stable `raw_id`; permalink is the only identifier | permalink is stable — use it directly |
| wire | `guid` is regenerated whenever an item is edited | `link` (the article URL) is stable — use it, not `guid` |

The canonical link is stored in the database.  The `UNIQUE` index on `items.link`
enforces uniqueness at the DB level for fresh databases; on older databases that
already contain duplicate links the index is skipped and the Python-level filter
handles deduplication (see `store.connect()`).

**Do not clear `digest.sqlite3` in production** unless you want every item in
the current feeds to be re-delivered as if it were new.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
