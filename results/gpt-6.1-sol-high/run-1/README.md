# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints only pending items instead of sending them. It still archives
feed observations, but does not record deliveries or consume pending items.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite observation archive and per-channel delivery history |
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

Every invocation must use the same persistent `--db` path (the default above is
`/srv/notify-digest/digest.sqlite3`). Losing or replacing that database loses
delivery history and makes currently visible items eligible again.

## Delivery decisions

Each successful digest records its items in `deliveries`, keyed by **channel
name, feed source name, and normalized article URL**. The `items` table remains
an observation archive, including repeated observations; seeing an article is
not proof that any channel received it.

* URLs are used because blogroll has no IDs and wire regenerates IDs on edits.
  URL fragments and `utm_*` query parameters are ignored, so changing campaign
  tags does not resend newsroom articles. Other query parameters are retained
  because they can identify different articles. The original URL is rendered.
* Titles, summaries, publication dates and raw IDs are not identity: corrections
  to an already delivered article do not trigger another notification. A changed
  article URL (beyond the normalization above) is treated as a new item. There is
  no cross-source story matching. Duplicate identities in one poll are sent once.
* Delivery history is per channel, not global. A new channel can receive current
  matching articles even if another channel already received them. Filter edits
  can expose previously unmatched articles, but do not resend delivered ones.
  Keep feed/channel names stable; renaming them starts a new identity/history.
* Selection, sending and recording run under a SQLite `BEGIN IMMEDIATE` write
  transaction. Overlapping jobs sharing this database serialize, then recheck
  history. A job waits up to 60 seconds for the database lock; if it cannot get
  it, it fails and the next cron tick can retry. Keep the DB on local disk.
* Each channel commits immediately after its send succeeds. If a later channel
  fails, earlier successes remain recorded. The failed channel and channels
  after it remain eligible on the next tick, provided the items are still in
  the feeds. The archive is not a retry queue for items that leave the feeds.

This is **at-least-once retry behavior**, not exactly-once external delivery.
Recording before sending would silently lose notifications on a failed send,
so we record afterward. A receiver may accept a webhook before a timeout, or the
process may die after sending but before the database commit; those ambiguous
sends can be repeated. Eliminating that window requires receiver-supported
idempotency, which the current webhook interface does not provide.

Existing databases are upgraded additively when opened. Historical archive rows
are deliberately not marked delivered: the old code never recorded which
channels succeeded. The first upgraded run therefore sends current matching
items once, then subsequent runs suppress them. Back up the database before
deployment; do not seed success history from observations alone.

## Verification

```sh
python3 -m unittest discover -s tests -v
```

The regression tests use local fixtures and temporary SQLite databases, with
delivery mocked so no webhooks are sent.
