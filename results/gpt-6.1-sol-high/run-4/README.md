# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints only pending items instead of sending them. It still archives
feed observations, but never records delivery, so a preview cannot consume items.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite observation archive and per-channel delivery receipts |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py --config /srv/notify-digest/config.toml --db /srv/notify-digest/digest.sqlite3 >> /var/log/digest.log 2>&1
```

Keep the database at a persistent, shared path for every invocation. Deleting it
or pointing cron at a different file resets delivery history. Back it up along
with the config.

## Why items no longer repeat

Previously every poll sent all matching items. The `items` table only recorded
observations, including repeated observations; it did not track successful sends.
There are now separate `deliveries` receipts keyed by **channel name, source name,
and stable item identity**. The archive remains an observation log.

Identity rules (implemented in `feeds.item_identity`):

* `newsroom`: use its stable `entry_id`, scoped to the configured source name.
* `blogroll` and `generic`: use the permalink, because blogroll lacks IDs and the
  wire regenerates GUIDs on edits. Lowercase scheme/host, remove fragments and
  known tracking parameters (`utm_*`, `fbclid`, `gclid`), and sort query pairs.
  Preserve other query parameters because they may identify different articles.
* Title, summary, and publication-time edits do not create new identities. We
  notify once per article, not once per revision. Different sources remain
  separate even when they cover the same story. URL-based feeds changing an
  article's actual permalink will look new; we do not guess using title matches.

Duplicate rows in a poll are collapsed before filtering. Each channel receives
its own matching, previously undelivered items, newest first. Empty digests are
not sent. Changing keywords can expose previously unmatched items still in the
feed, but does not resend delivered ones. Renaming a channel or source starts a
new history namespace; keep names stable when editing destinations or feed URLs.

### Delivery and failure tradeoffs

Each channel's selection, send, and receipt insert run under one SQLite
`BEGIN IMMEDIATE` transaction. This serializes overlapping processes using the
same database. We deliberately hold the write lock over the send: checking and
then unlocking before sending would allow two cron processes to send the same
items. SQLite's default five-second lock timeout can make a competing run fail
with `database is locked`; the next cron tick retries.

Receipts commit **after** a successful send, separately for each channel. If a
later channel fails, earlier successes remain recorded. The failure still exits
nonzero, and the next run retries unrecorded items that remain in the feeds.
There is no durable outgoing queue: an item disappearing from the feed before a
successful delivery is not retried from the archive.

This favors retry over silent loss. A webhook may accept a request and then time
out, or the process may die after sending but before committing its receipts.
Either can produce a duplicate on retry. Exactly-once external delivery needs
receiver-supported idempotency; the current webhook protocol has none. Marking
before sending would instead lose notifications on failure.

### Existing databases and rollout

Startup adds the receipts table without rewriting or removing the archive.
Old archive rows cannot prove which channels received an item, so they are not
backfilled as deliveries. **The first live run after upgrading sends all current
matches once**, including items people may already have received. Subsequent
runs use receipts. Run `--dry-run` first to preview this initial batch. Roll out
after stopping any already-running old process, which has no receipt checks.

## Verification

```sh
python3 -m unittest -v
```

Tests use temporary SQLite databases and mocked delivery, without network access.
They cover persisted reruns, snapshot A → B (only the new union story is sent to
ops and everything), edits and tracking URLs, duplicates within a poll, previews,
filter/channel changes, partial delivery and feed failures, existing archives,
and overlapping runs.
