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
| `store.py` | SQLite archive + dedup (only new items are sent) |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deduplication

The feeds keep serving old items, so on every 15-minute run we only send items
we have **not sent before**. `store.py` records each item the first time it is
seen; `digest.py` only forwards items that are new to that table.

An item's identity is `(source, dedup_key)`, where `dedup_key` is the item's
**link with the query string and fragment stripped**. We deliberately use the
link, not the provider id, because the link is the only field that is stable
across all three feeds:

- `blogroll` has no id at all, only a permalink;
- the `wire` feed regenerates its `guid` whenever an item is edited, so the
  guid is not a reliable identity (the link is);
- the `newsroom` feed appends rotating `utm_*` params to its URLs, so we strip
  the query string to get a stable key.

Consequences:

- An item that is *edited* (new guid, new title, same link) is the same item
  and is **not** re-sent.
- If a feed ever reuses one link for different content, that content is
  collapsed into one item. If that bites you, change `item_key()` in `store.py`
  (e.g. include the provider id for feeds whose ids are stable).

`--dry-run` checks what is new without recording it, so a preview does not
consume items — the next real run still sends them.

Existing databases are migrated automatically: the `dedup_key` column is
added, backfilled, and the duplicate rows the old code accumulated are
collapsed (earliest copy kept). This is a no-op on fresh databases.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
