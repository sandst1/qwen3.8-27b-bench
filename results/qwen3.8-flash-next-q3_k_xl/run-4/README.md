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
| `store.py` | SQLite archive of everything seen + per-channel delivery ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deduplication: why you don't get the same items twice

Each cron tick only sends a channel the items it has not already received.
`store.py` keeps a `deliveries(channel, item_key)` table; `digest.py` skips
anything already in it and records a row *after* each successful send. A
failed send records nothing, so the next tick retries it (at-least-once,
never silently dropped).

`item_key` (in `feeds.py`) identifies an item by **source + normalised URL
path**, deliberately *not* by the provider's id, because none of the three
providers offer something we can trust across all of them:

| Provider | Why its id doesn't work |
| --- | --- |
| newsroom | `entry_id` is fine, but links carry rotating `?utm_campaign=wNN` params — the query string is stripped so edits don't look new |
| blogroll | no id at all; permalink is stable even when the post is edited |
| wire ("generic") | `guid` is regenerated every time an item is edited |

Trade-off: an item edited after delivery is *not* re-sent (we key on the
URL, not the content), and if a provider ever distinguishes items by query
string, `feeds.item_key()` needs revisiting.

**First run after deploying this fix:** the ledger starts empty, so every
item currently in the feeds is sent once more. To avoid that, backfill:

```sh
python3 - <<'PY'
import digest, feeds, store
cfg = digest.load_config("config.toml")
db = store.connect("digest.sqlite3")
items = [i for f in cfg["feeds"] for i in feeds.fetch(f)]
for ch in cfg["channels"]:
    keys = {feeds.item_key(i) for i in items if digest.matches(i, ch)}
    store.record_deliveries(db, ch["name"], keys)
PY
```

(or just accept one last full send).

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
