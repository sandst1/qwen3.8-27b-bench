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
| `store.py` | SQLite archive + per-channel delivery ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## How duplicate suppression works

Cron runs every 15 minutes but feeds change slowly, so almost every run refetches
stories that were already sent. Each run delivers only items a given channel has
**not already received**. Two decisions make that work, and both are easy to get
wrong, so they are called out here.

**1. Dedup is per channel.** The `delivered` table in `store.py` keys on
`(channel, dedup_key)`. A story that matches only `ops` must stay eligible for
`energy`, and a channel that was down and retried must not silently lose its
items. We also record a delivery only *after* `channels.send()` returns, so a
failed send is retried on the next tick rather than marked as done.

**2. Identity is the canonical link, not the provider's id.** This is the subtle
one. None of the three providers offers a field you can trust on its own — the
two snapshots in `fixtures/` show each one lying in a different way:

| Feed | Stable | Changes between snapshots |
| --- | --- | --- |
| newsroom | `entry_id` | URL tracking params (`utm_campaign=w33`→`w34`) |
| wire | `link` | `guid` is regenerated on edits (`...0031`→`...0031-r2`) |
| blogroll | `permalink` | no id at all; title edited (`... (updated)`) |

So `raw_id` is wrong for two of three feeds and the raw `link` is wrong for
newsroom. `feeds.item_key()` builds the key from the **source + link with
tracking params and the fragment stripped**, which is the one signal that stays
put across all three. If you add a feed that publishes a genuinely stable id,
prefer it in `item_key()` — but only a stable one; a regenerating guid is worse
than the link.

The `items` table is a separate concern: an archive of every distinct story ever
seen (`INSERT OR IGNORE` on the same key), kept so we can answer "did this ever
come through?". It is not read by the send path.

Old databases are migrated in place on startup (`store._migrate`): the pre-dedup
`items` table gains a `dedup_key` column without losing rows, and the `delivered`
ledger starts empty, so the first run after deploying may resend once.

## Tests

```sh
python3 test_dedup.py
```

No third-party dependencies. The tests drive the two fixtures snapshots through
`run_once` and assert that edited / re-id'd / re-tracked repeats are never
resent, that only the genuinely new story goes out, and that a pre-dedup
database migrates cleanly.


## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
