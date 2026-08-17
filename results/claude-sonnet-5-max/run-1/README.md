# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it, and — importantly —
does not mark anything as seen (see "Dedup" below), so it's safe to run
repeatedly to preview without affecting what a real run later sends.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats, and computing each item's `dedupe_key` |
| `store.py` | SQLite archive of everything seen, and the dedup ledger `filter_unseen` checks |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`. The two snapshots aren't just "some items added" —
they specifically exercise the identity quirks described below (same story,
different tracking-param URL; same story, edited, no id; same story, edited,
regenerated guid). `test_digest.py` runs against both.

## Dedup

This runs from cron every 15 minutes (see Deployment below), and a feed's
"current window" typically still contains items from the last run or several
runs ago. Originally nothing tracked what had already been sent, so the same
item was re-sent every tick for as long as it stayed in the feed. Fixed by:

1. `feeds.fetch()` computes a `dedupe_key` per item.
2. `store.filter_unseen()` checks those keys against the archive before
   anything is considered for a digest.
3. `digest.run_once()` records the keys of whatever it just processed —
   *after* attempting delivery, and only on a real (non-dry-run) run.

The one thing to understand if you touch this: **`dedupe_key` is not just
`raw_id`.** The three feed formats have very different id reliability:

| Format | `raw_id` | What we key on | Why |
| --- | --- | --- | --- |
| `newsroom` | `entry_id` | `raw_id` | A real primary key; stable across edits/retitles. Its URL is *not* stable (carries `utm_campaign` etc. that changes per export). |
| `blogroll` | none | canonicalized link | No id is ever provided. The permalink is the only identity available, so an edited post (new title/excerpt, same permalink) is treated as the same item. |
| `generic` (wire) | `guid` | canonicalized link | The provider regenerates the guid on every edit (typo fixes, retagging), so trusting it would mean trivial edits get re-sent as "new". The link is what's actually stable here. |

"Canonicalized link" means query string and fragment stripped (`feeds._canonical_link`).
A single global rule ("always use raw_id", or "always use the link") fails at
least one of these three formats — that's what the fixtures are for, and what
`test_digest.py` checks.

Two deliberate scope decisions, so nobody has to rediscover them by reading
the diff:

- **"Seen" is global, not per-channel.** An item is recorded once it's been
  through a run, regardless of which channels (if any) it matched. If you
  edit a channel's `keywords` later, items already seen under the old
  filter will not retroactively appear even though they'd now match — we
  judged this simpler and more predictable than tracking sent-state per
  (channel, item).
- **A mid-run delivery failure does not mark that run's items as seen.**
  `channels.send` treats delivery as best-effort and lets exceptions
  propagate so cron retries on the next tick; `record_items` runs after the
  channel loop, so an exception skips it entirely. If channel 2 of 3 fails,
  channel 1 (which already succeeded) gets a duplicate on the retry. That
  duplicate is exactly the bug this fix addresses, but we accept it in this
  one case because the alternative — marking items seen before confirming
  delivery — would mean a channel outage silently drops items forever
  instead of retrying them.

### Existing/production database

`store.connect()` migrates an existing `digest.sqlite3` created before this
fix (adds the `dedupe_key` column if missing). Rows recorded before the
migration have no `dedupe_key` and so cannot dedupe against future fetches —
in practice that means whatever was already in-flight at deploy time may be
sent one extra time, and every run after that dedupes normally. See the
comment on `store._ensure_dedupe_key_column` for why we didn't try to
backfill those rows instead.

## Tests

```sh
python3 -m unittest -v
```

`test_digest.py` runs `digest.run_once` against the fixtures end-to-end
(feeds -> dedup -> render -> stdout channel, real sqlite db in a temp dir) and
checks the three dedup traps above plus the `--dry-run` non-consumption
guarantee. There's no mocking: if this passes, an actual `digest.py` run
behaves the same way.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```

