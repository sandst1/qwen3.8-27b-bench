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
| `store.py` | SQLite archive of everything seen, and dedup/delivery tracking |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Avoiding repeat sends

Cron runs this every 15 minutes, and every feed re-lists items it has shown
before, not just new ones (that's just how these feeds work). So the same
item shows up across many consecutive fetches, and something has to decide
"has this channel already been sent this?" or people get notified over and
over for the same item.

Two things make that possible, both in `store.py`:

- **A stable identity per item.** The obvious choice, the provider's own id
  (`raw_id`), doesn't work: the `blogroll` feed doesn't have one, and the
  `generic`/wire feed regenerates its `guid` every time an item is edited
  (typo fixes, added detail), which would make every correction look like a
  new item. What *is* stable across all three feeds is the article link,
  once tracking query params (`utm_*` etc., which providers vary between
  fetches) are stripped. That's `feeds.dedupe_key()` / `normalize_link()` —
  read the docstring there before changing how items are identified.
- **Per-channel delivery tracking.** The `deliveries` table records
  `(channel, dedupe_key)` once a send actually succeeds. A digest for a
  channel only ever includes items missing from that table for that
  channel. It's per-channel, not global, because one item can match
  several channels' keyword filters and each of them needs to see it once,
  independently — see `store.unsent()` / `store.mark_sent()` and their one
  call site in `digest.run_once`.

The `items` table itself is now also deduped on `dedupe_key` (`INSERT OR
IGNORE`), so it stays one row per item instead of growing a new row every
15 minutes for the same items.

`--dry-run` still shows only what's actually unsent (so it's a true preview
of what a real run would do), but it never calls `mark_sent`, so it can't
cause a real send to be skipped later.

If a channel's `send()` fails (see `channels.py`), it's never marked as
delivered, so that channel gets another shot at those items on the next
cron tick — same "best effort" retry behaviour as before, just now
per-channel instead of accidentally-forever for everyone.

### Deploying this to the existing cron job

The utility box already has a `digest.sqlite3` from before this fix, with
the old `items` schema (no `dedupe_key`) and, thanks to the bug, likely
many duplicate rows per real item. `store.connect()` detects that on open,
adds the column, backfills it, and collapses duplicates down to one row per
`dedupe_key` before the new unique index is created — so upgrading in place
won't crash cron or fail to migrate. See `store._migrate_legacy_items`.

That old db has no `deliveries` history, though (the old code never tracked
per-channel sends), so the first run after deploying this fix will treat
everything currently archived as unsent and send it once per matching
channel — a one-time, bounded catch-up digest. Every run after that is
accurate. If that first catch-up digest is unwanted, truncate `deliveries`'
input by clearing `items` (or just point `--db` at a fresh file) before
deploying instead.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
