# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints only currently eligible items instead of sending them. It
still archives fetched observations, but never marks anything delivered.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite observation archive and per-channel delivery ledger |
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

Keep using the same persistent database (`--db`); relative paths are resolved
from the working directory. Deleting or changing the database resets delivery
history.

## Duplicate prevention and delivery semantics

Previously, every poll appended to `items` and sent every matching item again;
the archive was never consulted and did not track delivery. The separate
`deliveries` table now records successful delivery for each **channel name,
source name, and canonical article URL**.

* Identity uses the permalink because blogroll has no IDs and wire regenerates
  IDs on edits. Titles, summaries, publication dates, and raw IDs are not part
  of identity: edits do not trigger another notification.
* Canonical URLs drop fragments and `utm_*` query parameters (case-insensitive),
  which change between polls. Other query parameters are retained because they
  may identify an article. We do not merge similar headlines or different
  sources. A changed permalink counts as new; URL aliases and redirects are
  not resolved.
* Duplicate identities in one poll are sent once per channel. Items are still
  filtered independently for every channel. An item delivered to `ops` can
  still be delivered to `energy` if it matches there later.
* Channel and source names are durable identifiers. Renaming either creates a
  new delivery scope. Changing a channel URL or filter with the same name keeps
  its history; a newly added channel receives matching items in the current
  feeds. There is no replay of items that have disappeared from the feeds.
* Each channel is recorded **after** `channels.send` returns successfully and
  committed immediately. A failure still aborts the run; the next cron tick
  retries the failed channel without resending earlier successful channels.
* SQLite `BEGIN IMMEDIATE` covers selection, sending, and recording. Overlapping
  processes sharing the database wait for the writer (up to 60 seconds) and
  then recheck history. A lock timeout fails the run so cron can retry. This
  deliberately holds a write lock during delivery to keep this small job's
  concurrency model simple.

This is retry-oriented delivery, not exactly-once delivery: if the receiver
accepts a webhook but the response is lost, or the process dies after sending
but before committing, the next run can resend it. Marking items before sending
would instead risk silently losing notifications. Eliminating that last window
requires receiver-side idempotency support, which the current webhook protocol
does not provide.

### Existing databases

The ledger is created automatically without changing or deleting the existing
`items` archive. Old archive rows cannot prove which channels received an item,
so they are **not** backfilled as deliveries. The first upgraded run sends the
currently matching items once; subsequent runs suppress them. Archive rows
continue to represent observations (including repeated polls), not unique
articles or confirmed deliveries.

## Verification

Run the regression suite with no network or third-party dependencies:

```sh
python3 -m unittest discover -s tests -v
```
