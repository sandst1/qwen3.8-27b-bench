# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints currently pending items instead of sending them. It still
archives fetched observations, but never records a successful delivery.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite observation archive and per-channel delivery ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |
| `test_digest.py` | Offline delivery and identity regression tests |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```

Keep using the same persistent `--db` path on every run. Delivery history lives
in that SQLite file; deleting it or using a fresh database starts delivery over.

## Delivery identity and guarantees

Previously, each poll archived its observations and then sent every matching
item again. The archive cannot tell us whether any particular channel received
an item. A separate `deliveries` table now tracks successful sends by
**channel name + feed source name + canonical article URL**.

The URL is the identity because blogroll has no ID, wire regenerates GUIDs on
edits, and titles/summaries can change. Canonicalization removes fragments,
`utm_*`, `fbclid`, and `gclid`, and lowercases the scheme/host. Other query
parameters, paths, and trailing slashes remain significant: dropping an `id`
parameter could silently suppress a different article. We deliberately avoid
fuzzy title matching or merging stories from different sources. A new article
URL is considered new; edits at the same URL are not sent again to a channel
that already received it. No redirects are resolved for identity.

Delivery history is per channel, not global. An item sent to `ops` remains
eligible for `energy`. A new channel receives all currently fetched items that
match its filter. Changing a filter may expose previously unsent items, but
does not resend already delivered ones. Treat channel and feed names as stable
identifiers: renaming them starts a new delivery history for that name.

Each channel's selection, send, and delivery record run under a SQLite
`BEGIN IMMEDIATE` transaction. This serializes overlapping cron runs, including
the network call. A competing writer waits up to 30 seconds and then fails
without sending if it cannot acquire the lock; cron can retry next tick.
Each successful channel commits immediately. If a later channel raises an
error, earlier successes stay recorded and the failed channel remains eligible
on the next poll. Retry candidates come from the current feeds, not the archive;
an item that disappears from its feed before a successful retry is not replayed.

We record delivery **after** `channels.send` returns successfully (stdout counts
as a delivery too). This favors retrying over silently dropping items. There is
an unavoidable uncertainty window if the receiver accepts a webhook but the
response is lost, or the process dies after sending and before the SQLite
commit. Such a batch may be sent again. Exactly-once delivery would require
receiver-side idempotency; a local transaction alone cannot guarantee it.

### Existing databases

The delivery table is created automatically alongside the existing archive;
archived rows are preserved. It starts empty because old observations are not
proof of successful delivery to any channel. **The first upgraded run sends all
currently matching items once**, then subsequent runs send only unsent items.
This explicit baseline avoids silently losing items based on guessed history.

## Verification

```sh
python3 -m unittest -v
```

The offline tests cover repeated polls across database connections, both fixture
snapshots (including tracking URL churn, retitles, and changed GUIDs), channel
failures and retries, dry runs, overlapping runs, duplicate input, filter changes,
new channels, feed outages, URL identity boundaries, and old databases.
