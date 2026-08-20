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
| `store.py` | SQLite archive of everything seen + the sent-item ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Dedup

Every run re-fetches and re-filters the full feeds from scratch — there's no
"since last run" fetch — so every item matching a channel's keywords would be
sent again on every 15-minute tick unless something stops it. `store.py`'s
`sent` table is that something: a (channel, source, raw_id) ledger, checked
before sending and updated after. `--dry-run` never writes to it, so previews
don't consume real sends.

The one subtlety is what `raw_id` means, because the three providers are
each unreliable in a different way (see `feeds.py`):

* **newsroom** hands out a stable `entry_id`, but its `url` carries a
  `utm_campaign` param that changes on re-fetch — so we key on `entry_id`,
  not the link.
* **blogroll** has no id field at all, but its `permalink` doesn't change
  when a post is edited in place — so we key on the permalink.
* **wire** (generic format) has a `guid`, but the provider mints a new one
  every time an item is edited (typo fixes, added detail); its `link` is
  what actually stays constant — so we key on the link, not the guid.

If a new feed format is added, figure out which of its fields is actually
stable across re-fetches of the same logical item before wiring up
`raw_id` — don't assume the field called "id" is it.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
