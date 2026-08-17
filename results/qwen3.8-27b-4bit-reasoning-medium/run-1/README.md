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
| `store.py` | SQLite: unique-item archive + `sent` delivery ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Dedup: why you don't get the same item twice

The job runs every 15 minutes, but each item is delivered to a given
channel at most once. How:

- `feeds.py` assigns every item a stable `identity`, namespaced by feed
  name. Each provider's natural id has a flaw — newsroom URLs rotate a
  `utm_campaign` param, blogroll has no id at all, wire's guid changes on
  every edit — so the identity is the most stable field per format. The
  per-format reasoning is in the `feeds.py` docstring.
- `store.py` keeps a `sent` table: one row per (item, channel) pair that
  has been delivered. A run filters out anything already in it.
- A `sent` row is written only *after* the channel accepts the digest. If
  a webhook is down, the exception propagates (as before) and the next
  tick retries just the channels that missed it — channels that already
  got the item are not re-sent.

Operational notes:

- The first run against a fresh (or deleted) db sends everything
  currently in the feeds, once. Don't delete `digest.sqlite3` casually.
- `--dry-run` reads the ledger but never writes to it.
- If an item's identity changes upstream (e.g. a permalink moves), it is
  treated as a new item and sent again.
- `sent` and the `items` archive grow monotonically; prune by `sent_at` /
  `first_seen` if that ever becomes a problem.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```

The db path must be stable between ticks (it is, in the cron line above);
the dedup ledger lives in it.
