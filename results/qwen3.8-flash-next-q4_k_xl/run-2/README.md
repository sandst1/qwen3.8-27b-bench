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
| `store.py` | SQLite: archive of seen items + per-channel delivery ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## How duplicate delivery is prevented

The job runs every 15 minutes but feeds keep re-listing old articles, so the
run loop has to remember what it already sent. Two tables in SQLite do this
(see `store.py`):

- **`delivered`** — one row per `(channel, source, dedup_key)` that was sent
  successfully. A channel never receives the same article twice. Tracking is
  *per channel*, not global: each recipient gets every article exactly once,
  and a channel added later still gets a first-time backfill of the items it
  matches. Rows are written **only after** `channels.send()` succeeds, so if a
  webhook is down the items are retried on the next tick instead of being lost.
- **`items`** — an archive of every distinct article ever seen, deduplicated on
  `(source, dedup_key)` so it stops growing on every poll.

### What counts as "the same article" (`feeds.item_key`)

This is the subtle part — none of the raw fields are a reliable id:

| Feed       | Stable? | Why the obvious key fails |
| ---------- | ------- | ------------------------- |
| newsroom   | `entry_id` | the `link` carries a `utm_campaign` that rotates weekly (`w33`→`w34`) |
| blogroll   | nothing  | no id at all; title/excerpt get edited in place |
| wire       | `link`   | the provider **regenerates `guid`** every time an item is edited |

The one field that stays constant for the same article across all three is the
**link with tracking params stripped** (`utm_*`, `fbclid`, …). `item_key()`
normalises that link and is always compared scoped by `source`. Consequence:
an edited article (new guid, new title, new utm tag) is correctly treated as
*already sent* and not re-notified — which is what we want.

Verify it with the fixtures (run 2 sends nothing; snapshot-b sends only the one
genuinely new article, not the edited wire/blogroll items):

```sh
sed 's/snapshot-a/snapshot-b/g' config.example.toml > /tmp/cfg-b.toml
python3 digest.py --config config.example.toml --db /tmp/d.db   # first run
python3 digest.py --config config.example.toml --db /tmp/d.db   # -> sent 0 items
python3 digest.py --config /tmp/cfg-b.toml     --db /tmp/d.db   # -> only the new item
```

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
