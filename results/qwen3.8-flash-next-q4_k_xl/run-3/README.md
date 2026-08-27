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
| `store.py` | SQLite archive of everything seen + per-channel delivery ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## How duplicates are prevented

Every item gets a `uid` (`"<source>|<key>"`) and a new `deliveries` table in
SQLite records one row per `(channel, uid)` after a successful send. A
channel only receives items whose uid is not yet in its ledger, so running
from cron every 15 minutes no longer re-sends the same stories.

Two decisions worth knowing before you change this:

**The dedup key is configured per feed (`dedupe_by`), because no single
field works for all three providers:**

| Feed | Stable | Unstable | `dedupe_by` |
| --- | --- | --- | --- |
| newsroom | `entry_id` | URL (rotating `utm_campaign` param) | `raw_id` |
| wire | link | `guid` (regenerated on every edit) | `link` |
| blogroll | permalink | — (no id at all) | `link` |

The fixtures demonstrate both traps: between `snapshot-a` and `snapshot-b`
the newsroom URLs change while entry ids stay the same, and the wire guid
changes while the link stays the same. Deduping on either field alone
resends something.

**Edits do not re-trigger a send.** A retitled blog post or corrected wire
story keeps its uid and is not re-notified. That is deliberate: re-sending
on every provider-side edit is what caused the original spam, and missing
an edit is the cheaper failure. If you ever want update notifications, add
a content hash to the ledger as a *separate* signal — do not weaken the
uid.

Delivery is marked only after the channel send succeeds, so a failing
webhook retries on the next tick instead of silently dropping items.
`--dry-run` never writes to the ledger.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
