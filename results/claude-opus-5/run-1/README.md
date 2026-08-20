# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it, and records
nothing — a dry run never suppresses a later real send.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

```sh
python3 -m unittest discover      # tests, no network needed
```

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching, normalising the three feed formats, **item identity** |
| `store.py` | SQLite archive + the per-channel delivery ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |
| `test_digest.py` | Tests, driven off the two fixture snapshots |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Not resending things

This is the part worth understanding before you change anything.

The job runs every 15 minutes and the feeds mostly return the same items each
time, so "have we already sent this?" is the central question. It is answered
by the `deliveries` table in `store.py`, keyed by **(channel, item key)**.

### Item identity

There is no single field that identifies an item across our three providers.
Each one breaks a different obvious choice — this is why the naive fixes
("just use the guid", "just use the link") do not work:

| provider | stable | unstable |
| --- | --- | --- |
| `newsroom` | `entry_id` | the url — `utm_campaign` is rotated weekly (`…w33` → `…w34`) |
| `blogroll` | `permalink` | title and excerpt are edited in place; there is no id field at all |
| `generic` (wire) | `link` | `guid` — regenerated on every edit (`wire-…-0031` → `…-0031-r2`) |

So identity is decided per format, in `feeds.py`:

* Trust the provider's id **only where that provider keeps it stable**
  (`newsroom`). For `generic` we deliberately ignore `guid`, because it is a
  revision id rather than an item id.
* Otherwise fall back to the link with tracking parameters stripped.
* Keys are namespaced by source, so two providers carrying the same story
  remain two items — channels expect to see both, attributed.

Titles and summaries are never part of the identity. Both `blogroll` and
`generic` edit them after publication, and treating an edit as a new item is
the bug we are avoiding. **The accepted trade-off:** a genuine correction to
an item is *not* re-sent to a channel that already saw the original. These are
digests, not alerts, so silence on an edit beats repeating the same story.

`test_digest.py` pins all three of these cases. If you simplify the rules in
`feeds.py`, those tests are what will tell you.

### Why the ledger is per channel

An item can match several channels. A global "already sent" record would let
the first channel to receive an item suppress it for every other channel, and
those channels would silently never see it.

### Delivery is at-least-once

Items are marked delivered **after** the channel send succeeds, not before:

* mark-then-send loses items permanently whenever a webhook is down.
* send-then-mark can resend one digest if the process dies in between.

A rare repeat after a crash is much cheaper than an item nobody ever sees. For
the same reason, a failing channel is logged and skipped rather than aborting
the run, so one dead webhook cannot hold up the others; the run then exits
non-zero so cron surfaces it.

### Adding a channel

A new channel has no delivery history, so its first run would send it every
item currently in the feeds. To start it from "now" instead:

```sh
python3 digest.py --config config.toml --db digest.sqlite3 --seed
```

`--seed` records everything currently in the feeds as delivered without
sending anything. It affects any channel with unsent items, so run it when
adding a channel, not routinely.

### Upgrading an existing database

`store.connect` migrates a pre-deduplication `digest.sqlite3` in place: it adds
`item_key`, collapses the duplicate archive rows the old code accumulated (one
per item per tick), and keeps the earliest `first_seen`.

The `deliveries` ledger necessarily starts empty, so **the first run after
upgrading sends one more full digest**, and every run after that is quiet. Use
`--seed` for that first run if you would rather nobody gets it.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
