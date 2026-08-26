# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it; it does not
touch the database.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite archive of everything seen + per-channel delivery log |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## How duplicate sends are prevented

Cron runs every 15 minutes and feeds overlap heavily between runs, so
"send everything the feed contains" (which is what this used to do) spams
people with the same items forever. Now: **an item is delivered to a channel
at most once, ever.** Before rendering, each channel's selection is filtered
against the `sent` table (`store.seen_keys`), and rows are written into it
(`store.mark_sent`) only *after* `channels.send()` succeeds — a failed
delivery leaves no trace and is retried on the next tick.

The dedupe key is `feeds.item_key()` = source + identity, where **identity is
picked per provider in `feeds.py`**, because no single feed field is stable
across all three (compare the two snapshots):

| Provider | Identity used | Why not the obvious choice |
| --- | --- | --- |
| newsroom | `entry_id` | URLs carry a rotating `utm_campaign` param |
| blogroll | `permalink` | no id exists; title/excerpt get edited |
| wire     | `link` | `guid` is regenerated on every edit |

When adding a provider, choose its identity in `feeds.fetch()` and say why
there. The fixtures exist to test exactly this.

Two known sharp edges, both accepted deliberately:

* **First run on a fresh DB floods.** Everything looks new. That is usually
  what you want; if not, seed the DB with `--dry-run`-style logic by hand.
* **Renaming a channel re-sends its history**, since `sent` rows are keyed by
  channel name. Rename rarely, or migrate the `sent` table when you do.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
