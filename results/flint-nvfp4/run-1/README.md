# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints only pending items instead of sending them. It still archives
fetched observations, but never records deliveries or consumes pending items.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite observation archive, item identity, and delivery ledger |
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

Keep using the **same persistent database path** on every run (use an absolute
`--db` path if the working directory varies). Deleting the database loses delivery
history and causes current feed items to be sent again.

## Deduplication decisions

The old loop archived observations but sent every matching item on every run.
The archive cannot tell us whether a send succeeded. A separate `deliveries`
table now records successful sends, keyed by **channel name, source name, and
canonical permalink**. The archive remains an append-only record of fetches.

* Permalinks work across all three providers: blogroll has no ID and wire changes
  its GUID when edited. Titles, summaries, publication dates, and raw IDs are not
  identity. Edits to an already delivered article do not trigger another send.
* Canonicalization removes fragments, `utm_*`, `fbclid`, and `gclid`, lowercases
  scheme/host, and sorts query pairs. Other query parameters (including blank
  values), path case, and trailing slashes remain significant. We do not merge
  HTTP with HTTPS or follow redirects. This deliberately avoids guessing that
  different article URLs refer to the same story. If a provider changes its
  actual permalink, it is treated as new; revisit identity rules with tests if
  that becomes a real provider behavior.
* Source names scope identity: two feeds covering the same story are independent.
  Duplicate identities within one poll are sent once per channel, using the
  first matching item in publication order.
* Channel names are persistent delivery identities. Keep feed and channel names
  unique and stable. A new/renamed channel receives all currently matching feed
  items. Changing a filter can send previously unmatched items, but does not
  resend items already delivered to that channel. Changing a channel's URL or
  title while keeping its name retains its history.

### Success, failure, and overlapping cron runs

Each channel's pending selection, send, and ledger update run under one SQLite
`BEGIN IMMEDIATE` transaction. The write lock prevents two processes sharing the
database from selecting and sending the same pending items concurrently. Feed
fetching happens outside this lock; competing writers wait up to 60 seconds
before failing for cron to retry. Successful channels commit independently.
If a later channel raises an error, its ledger update rolls back; the next run
retries it without repeating earlier successful channels. As before, a delivery
error stops the run, so subsequent channels wait for the next invocation.

We record delivery **after** `channels.send` returns successfully, to avoid
silently losing items on failed sends. This is **at-least-once**, not exactly-once:
a webhook might accept a request and then time out, or the process might die
after sending but before committing. Such ambiguous failures can still repeat
a digest. SQLite cannot atomically commit with an external webhook; exactly-once
would require receiver-side idempotency support. Failed items are retried only
while they remain in the fetched feeds; the archive is not a delivery queue.

### Existing databases / rollout

Startup adds the ledger table without modifying or deleting existing archive
rows. We deliberately do not backfill delivery history from the archive:
"fetched" does not mean "delivered", especially after errors or dry runs. The
first real run after upgrading therefore sends all currently matching items
once, then subsequent runs send only new items per channel. Preview that batch
with `--dry-run`. Keep the ledger alongside the archive in database backups.

## Verification

```sh
python3 -m unittest discover -s tests -v
```

The offline regression suite covers both fixture snapshots, persistence across
connections, per-channel history and retries, dry runs, old databases, URL
identity, within-poll duplicates, and overlapping runs.
