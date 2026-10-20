# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints only pending, matching items. It neither sends nor records
observations or delivery receipts (opening a new database still creates tables).

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite observation archive, article identity, delivery ledger |
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

Keep using the same persistent `--db` path on every run; deleting the database
also deletes delivery history. The default is relative to the working directory.

## Duplicate prevention and delivery policy

Previously the job archived observations but sent every matching fetched item
on every run. The archive is not a delivery log: a feed item may have been
filtered out, previewed, or followed by a failed send.

The `deliveries` table now records success for **(channel name, source name,
normalized article URL)**. Selection, sending, and recording success happen
under one SQLite `BEGIN IMMEDIATE` transaction per channel. Successful channels
commit independently; if a later channel fails, the next cron run skips earlier
successes and retries unconfirmed deliveries. The existing fail-fast behavior
remains: a failing channel delays subsequent channels until a later run.

### What counts as the same item?

- Use the article URL rather than raw IDs: blogroll has no IDs and wire GUIDs
  change when an item is edited. Titles, summaries, and dates are not identity.
- Lowercase URL scheme/host; strip fragments and `utm_*` query parameters.
  Preserve paths, trailing slashes, scheme differences, and all other query
  parameters (including their order): they may distinguish real articles.
  Display the original URL in the digest.
- Scope identity to the feed's configured `name`. Similar stories from different
  sources remain separate; there is no fuzzy title/story matching.
- Collapse repeated observations of that identity within a digest, using the
  latest observation. Edits are archived but do not trigger another delivery to
  a channel that already received the article.
- Scope delivery to the channel's configured `name`, so one channel never hides
  an item from another. Keep names stable: renaming a channel/feed resets its
  identity. Changing a channel's URL/type keeps its history; use a new channel
  name when a new destination should receive the backlog.

Selection uses the latest archived version of every article, not just the current
feed page. This preserves retries even if an article drops out of the feed or
fetching fails. Filters apply to that latest version; newly matching articles and
new channels can receive older archived items. The observation archive still
appends every poll, as before; this small tool currently scans it for selection.

### Failures, overlapping cron runs, and upgrade

Receipts are written **after** a successful send. A failed send leaves the batch
pending. A remote webhook and SQLite cannot commit atomically: if the receiver
accepts a message but its response is lost, or the process crashes before the
receipt commits, a retry can duplicate that batch. This chooses retryability
over marking items sent before delivery and silently losing them. Exactly-once
remote delivery would require receiver-side idempotency support.

Holding SQLite's writer lock through delivery prevents overlapping runs using
the same database from both selecting the same pending batch. Contenders wait
up to SQLite's default five-second timeout, then exit with a lock error if the
other run is still busy; cron retries later. Network sending has a timeout in
`channels.py`. Do not use separate database copies for concurrent workers.

Existing databases are upgraded additively on open; the `items` table and its
observations are preserved. There is no reliable way to infer past delivery
from old observations, so no receipts are fabricated. **The first upgraded run
can send the matching archived backlog once**, including items that were already
sent by the old code. Subsequent successful deliveries are suppressed normally.

## Verification

```sh
python3 -m unittest -v
```

The offline regressions exercise both fixture snapshots, process restarts,
within-poll duplicates, per-channel failures and retries after feed expiry,
filter/channel changes, dry runs, legacy database upgrade, and overlapping runs.
Snapshot A sends 12 item/channel deliveries; repeating A sends zero; moving to B
sends only the new union article to `ops` and `everything` (two deliveries).
