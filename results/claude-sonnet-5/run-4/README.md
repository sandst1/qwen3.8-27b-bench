# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it, and never marks
items as notified (see "Deduplication" below).

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite archive + notified-item tracking |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`. Diff them to see the "same" item change guid,
url, or text between polls — that's exactly the case dedup has to survive.

## Deduplication

digest.py runs every 15 minutes from cron, re-fetching the *entire* feed
each time (feeds are not paginated by "since last poll"). Without dedup that
means every item gets re-sent on every tick, forever — which is what this
codebase used to do.

Each item now carries a `dedupe_key` (computed in `feeds.py`) and
`store.py` keeps a `notified` table of `(source, dedupe_key)` pairs that
have already gone out. `run_once` skips anything already in that table and
records new items there right after building the digest.

`dedupe_key` is deliberately *not* always the feed's own id/guid, because
none of the three providers give us something both stable and unique:

  * `newsroom` — `entry_id` is stable, but the item's `url` has a
    `utm_campaign` param that changes weekly. Key: `entry_id`.
  * `blogroll` — no id field at all, but `permalink` doesn't change even
    when the title/excerpt get edited after publishing. Key: `link`.
  * `generic` (wire) — has a `guid`, but the provider regenerates it on
    every edit (typo fixes, added tags, retitles). `link` is the part that
    stays put. Key: `link`.

An item is marked notified as soon as it's been evaluated against all
channels for a run, even if it didn't match any channel's keywords. The
alternative — only marking items that actually matched a channel — would
let an item resurface as "new" if someone later widens a channel's keyword
list, which is more surprising than the current behavior (an item that's
already cycled through the pipeline once stays gone).

The original `items` table is unchanged: it's still a pure archive of
everything ever seen (keyed by autoincrement id, not dedupe_key), for
"did this ever come through?" questions. It plays no part in suppressing
repeats.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
