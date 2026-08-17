# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it, and never marks
items as delivered (see "Duplicate sends" below) - it's a pure preview and
can be run repeatedly without affecting what the next real run sends.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats, and computing each item's `dedupe_key` |
| `store.py` | SQLite archive of everything seen, plus the per-channel delivery ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`. `snapshot-b` deliberately re-uses/edits items
from `snapshot-a` (query-param churn, a re-guid'd wire story, an edited blog
post) to exercise the dedup logic described below - run the tool against `a`
then `b` with the same `--db` to see it in action.

## Duplicate sends

Cron polls every 15 minutes, but feeds return a rolling snapshot of recent
items rather than "what's new since your last poll". Without any extra
bookkeeping, the same item sails through the keyword filter and gets
re-sent on every single tick until it eventually ages out of the upstream
feed - this was the original bug ("people keep getting the same items over
and over").

The fix has two parts:

1. **Identity (`feeds.py`)** - every item gets a `dedupe_key`. We deliberately
   do *not* use the provider's own id (`raw_id`) for this, because it isn't
   trustworthy across all three formats:
   - `newsroom`'s id is stable, but its link carries a `utm_campaign` tag
     that changes between crawls of the *same* story.
   - `blogroll` has no id at all.
   - `generic` (wire) has a `guid`, but the provider mints a new one every
     time the story is edited (typo fixes, added tags, retitles) - so a
     "new guid" doesn't mean "new story".

   The one thing all three providers keep pointing at the same story is the
   link, once you strip crawl-to-crawl tracking noise (`utm_*` params).
   `dedupe_key` is that normalized link, namespaced by feed name. One rule,
   no per-format branching outside of `feeds.py`.

   Trade-off this implies: if a story is edited after we've already sent it
   (typo fix, minor rewrite), it will **not** be re-sent, because from a
   reader's perspective a lightly-touched-up rehash of something they
   already got *is* the "same item over and over" complaint. If that ever
   needs to change (e.g. re-notify on substantial rewrites), it needs a
   deliberate content-hash-based mechanism - don't repurpose `raw_id` for it.

2. **Delivery ledger (`store.py`)** - a `deliveries` table records
   `(channel, dedupe_key)` the moment an item is actually sent to a channel.
   Before sending, `digest.py` drops anything already in that table for the
   channel in question. It's keyed per-channel (not globally) because the
   same item can legitimately reach more than one channel - e.g. a story
   matching both a narrow keyword channel and the keywords-less "everything"
   firehose should still reach both, once each.

   Delivery is only recorded *after* `channels.send()` returns successfully,
   so a channel outage doesn't get an item wrongly marked as delivered - per
   `channels.py`, send failures propagate and cron simply tries again next
   tick, and the item is still eligible to send at that point.

This is separate from the pre-existing `items` archive table, which keeps
recording every fetched item unconditionally (no dedup) as a "did this ever
come through" audit log; it doesn't influence what gets sent and its
continued unbounded growth is an existing tradeoff, not something this fix
changes.

Operational note: the first run after deploying this fix starts from an
empty `deliveries` table, so it will (re-)send everything currently
matching each channel's filters, same as any brand-new deployment would.
Behaviour is only "no repeats" from the second run onward.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```

The same `--db` path must be reused across ticks (it is, by default) since
that's where delivery state lives - wiping the database, or pointing at a
fresh one, means everyone gets a fresh flood of "new" items once.
