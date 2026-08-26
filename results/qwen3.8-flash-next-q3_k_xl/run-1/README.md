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
| `feeds.py` | Fetching, normalising the three feed formats, item identity |
| `store.py` | SQLite: archive of everything seen + per-channel deliveries |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Not sending the same thing twice

Cron runs every 15 minutes and feeds keep serving items they have already
served, so the job remembers what it has sent. A `deliveries` table (see
`store.py`) holds one row per `(channel, item key)`; before sending, a channel
only sees items it has not received yet, and rows are written only after a
channel accepts the digest — so a channel that is down retries next tick
instead of losing the item.

The subtle part is what counts as "the same item". No feed field works as a
blind key, so `feeds.py` computes a `key` per format and explains each choice;
the fixtures show the drift that motivated it:

- **newsroom** — keys on `entry_id`. The URL is *not* stable: `utm_campaign`
  is rewritten on every fetch, so keying on the link re-sends every story.
- **wire (generic)** — keys on the link. The `guid` is *not* stable: it is
  regenerated whenever an item is edited (`...0031` → `...0031-r2` in
  snapshot-b), so keying on the guid re-sends edited stories.
- **blogroll** — keys on the permalink. There is no id at all and titles get
  edited, so the permalink is the only durable field.

Tracking is per channel: two channels can each deliver the same item once, but
neither will repeat it. `--dry-run` applies the same filter and does not write,
so it shows what a real run would newly send.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
