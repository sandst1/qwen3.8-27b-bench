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
| `store.py` | SQLite archive of everything seen + per-channel sent ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

## Deduplication (why you don't get the same item twice)

Cron runs every 15 minutes and feeds return their whole recent window, so
each tick used to resend everything. `run_once` now consults a `sent`
table in SQLite (one row per channel + item) and only delivers items the
channel has not already received. Delivery state is written *after* a
successful send, so if a webhook is down the items are retried next tick.

Item identity is the **link with `utm_*` query params stripped**
(`feeds.item_key`), not the provider's id. The fixtures demonstrate why:

- newsroom: `entry_id` is stable but the link rotates `utm_campaign`
  weekly — fixed by normalising the link;
- wire: the link is stable but the provider **regenerates `guid` on every
  edit** — keying on `raw_id` would resend edited items;
- blogroll: ships no id at all; the permalink is the only handle.

The normalised link is the one identity that works for all three.
Accepted trade-off: an edit to an already-delivered item (retitled blog
post, wire "-r2" guid) is *not* resent. If we ever want "notify on
significant updates", that's a new feature to design, not a side effect
of the dedup key.


The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
