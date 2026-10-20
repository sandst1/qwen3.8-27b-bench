# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints only items not yet delivered to each channel. It still archives
feed observations, but does not send or record delivery receipts.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite observation archive, article identity, and delivery ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```

Keep using the same persistent database on every run (use an absolute `--db`
path if the working directory varies). Deleting it loses delivery history.

## Why items are sent only once per channel

Previously every poll sent every matching item; the archive was never consulted.
The `deliveries` table now stores a receipt for each **channel name + source name
+ normalized article URL**. Duplicate rows within a poll are collapsed too.

We use article URLs because blogroll has no IDs and wire regenerates GUIDs on
edits. Titles, summaries, and publication dates are content, not identity: editing
them must not trigger another notification. URL normalization removes fragments
and `utm_*`, `fbclid`, and `gclid` tracking parameters, lowercases scheme/host,
and sorts the remaining query parameters. Paths and other query parameters are
preserved to avoid merging different articles. The rendered link stays original.
Different sources remain separate even if they link to the same URL; this is not
cross-publisher story matching. A genuinely changed permalink is a new identity.
Changing normalization later requires considering the existing receipts.

Each channel is independent: sending to `ops` never suppresses `energy`.
Changing filters allows matching, previously unsent items still in the current
feeds through. Renaming a channel or source creates a new identity and can resend
items; keep names stable. Changing a channel's destination while keeping its name
retains its history.

### Failures and overlapping cron runs

Receipts are committed **after** a successful send, separately for each channel.
If a later channel fails, earlier successes remain recorded. The exception still
propagates, so cron/logs report the failure; the next run retries unacknowledged
items that are still present in the feeds. The archive is not a pending-delivery
queue: an item that disappears before successful delivery is not retried.

A SQLite `BEGIN IMMEDIATE` transaction covers receipt lookup, sending, and receipt
insertion. This serializes delivery across processes sharing the database, so
overlapping cron invocations cannot both send based on stale receipt lookups.
The write-lock timeout is 60 seconds; a longer wait fails the run for cron to
retry. Network fetching happens outside this transaction. Holding a write lock
during delivery is a deliberate simple tradeoff for this small cron job.

This is **at-least-once delivery**, not exactly-once. A webhook might accept a
request whose response is lost, or the process might die after sending but before
committing its receipts. Such a message can be repeated on retry. Recording before
sending would instead silently lose messages on failure; we prefer retrying.
Exactly-once would require receiver-supported idempotency, which the current
webhook protocol does not provide. Successful stdout sends are tracked as well.

### Existing databases

Startup adds the ledger without changing or deleting existing archive rows.
Those rows prove only that an item was fetched, not which channels received it,
so they cannot safely be backfilled as receipts. The first upgraded run sends the
currently matching items once, then subsequent runs suppress them. Back up the
database before deployment if you need to preserve the observation history.

## Verification

```sh
python3 -m unittest discover -s tests -v
```

The regression suite uses both fixture snapshots, a database reopened between
runs, failed deliveries, dry runs, existing-schema upgrade, and overlapping runs.
