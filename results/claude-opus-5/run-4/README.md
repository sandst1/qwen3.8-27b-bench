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
| `feeds.py` | Fetching, normalising the three feed formats, and item identity |
| `store.py` | SQLite archive + the record of what has been sent where |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`. The two snapshots are not arbitrary: `snapshot-b`
adds one genuinely new article and *edits* two existing ones, which is the case
deduplication has to get right. Use them when you touch any of this.

## Deduplication

Cron polls every 15 minutes and the feeds return the same items each time, so
the job has to decide which items it has already sent. Two things make that
harder than it looks.

**1. There is no single field that identifies an item.** Each of the three
providers breaks a different obvious choice:

| provider | `raw_id` | `link` | `title` |
| --- | --- | --- | --- |
| `newsroom` | stable | **rotates** — `utm_campaign` changes weekly | stable |
| `blogroll` | **absent** | stable | **edited in place** |
| `generic` | **regenerated on every edit** | stable | stable |

So "use the guid" spams from `blogroll` and `generic`, "use the link" spams
from `newsroom` every week, and "hash the content" spams whenever anyone fixes
a typo. Identity is therefore chosen **per format**, in `feeds.py`, next to the
quirk it works around: `newsroom` keys on its stable `entry_id`, the other two
key on the link with tracking parameters stripped. Each item carries the result
as `dedupe_key`.

Keys are only compared within one source, since a key like `id:84121` means
nothing outside the feed that issued it.

**2. "Already sent" is per channel, not global.** An item can match several
channels, so `deliveries` is keyed by `(channel, source, dedupe_key)`. Marking
an item sent globally would mean the `everything` firehose silently lost
anything `ops` happened to receive first.

### Consequences worth knowing

- **Edits do not re-notify.** A post edited after it went out stays quiet. This
  is the point: `generic` regenerates its guid on every edit, so treating edits
  as new items is precisely the bug that was reported. If a feed ever needs
  edits to re-notify, that has to be opt-in per feed, not the default.
- **One last duplicate on first deploy.** There is no delivery history for
  items sent before this existed, so the first run re-sends whatever is
  currently in the feed window. It settles from the second run on.
- **Delivery records expire after 90 days** (`DELIVERY_RETENTION_DAYS` in
  `store.py`). Do not lower this below the longest time a feed keeps an item
  visible — an item still in a feed after its record is pruned looks new and
  gets sent again.
- **A send is recorded only after it succeeds**, and a failing channel is
  logged and skipped rather than aborting the run, so one broken webhook no
  longer stops the channels after it. A crash between sending and recording
  repeats that digest once; the alternative ordering would drop it silently
  forever, which is worse for a digest.
- Links are normalised for *identity only*. Digests still show the publisher's
  original URL, tracking parameters and all.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```

The database is now load-bearing rather than just an archive: if you delete or
move `digest.sqlite3`, the next run re-sends the current feed window.
