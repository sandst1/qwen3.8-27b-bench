# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` is a pure preview: it prints what would be sent and writes
nothing to the database (no archive rows, no ledger marks).

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

Tests: `python3 -m unittest test_digest -v` (runs offline against `fixtures/`).

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite archive of seen items + per-channel delivery ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |
| `test_digest.py` | Checks for the dedup rules below |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`. They differ exactly where the dedup rules get
interesting: one new story, one retitled post, one edited item with a new
`guid`, and rotated utm tags on re-listed URLs.

## Keeping digests fresh (why digests used to repeat)

Feeds re-list their back-catalogue on every fetch, and cron ticks every 15
minutes, so a run that sends "everything the filters match" re-sends the same
items until the feed rotates them out. That was the original behaviour: people
got the same stories in every digest, forever. Since the fix, each run sends
only items that are **new for the receiving channel**, and two rules make
"new" mean *never sent before* rather than *never sent today*:

- **Identity is the item's link with the query string stripped** — the
  `generic` provider regenerates `guid`s whenever an item is edited and the
  `blogroll` provider has no ids at all, and the newsroom rotates utm tags in
  the query string (`utm_campaign=w33` becomes `w34` the next week). Ids and
  raw URLs would therefore resurrect edited stories on every edit or tag
  rotation. Never key dedup on `raw_id` or on the unnormalised link;
  `store.identity()` exists to be the single place that decides identity, and
  `test_digest.py` pins the behaviour.
- **The ledger is per channel** (`delivered` table: channel, source, key),
  because channels are independent views with different filters. A side
  effect: an item matching two filters appears once in *each* digest (the
  firehose channel will re-list what ops already saw). That is deliberate —
  the alternative (send-to-first-matching-channel) makes each channel's
  content depend on config order and starves broader filters of content.
  Revisit only if channel audiences turn out to overlap.

Delivery state is recorded *after* a successful send, committed per channel,
so a failing webhook retries its items on the next tick instead of silently
swallowing them.

**Upgrading a pre-fix database:** the fix seeds the empty ledger from the old
archive on its first run (anything already archived had already been spammed
to everyone every tick, so the upgrade run stays quiet instead of re-sending
the whole feed window one last time). A fresh database has nothing archived,
so the very first run sends the current feed window as a catch-up and goes
quiet from the second tick.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
