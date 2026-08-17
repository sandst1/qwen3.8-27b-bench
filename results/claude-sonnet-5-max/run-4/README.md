# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it. Note that
`--dry-run` never updates the "already delivered" state (see below), so
running it repeatedly always shows the same thing — that's expected, it's a
preview, not a tick.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite archive + per-channel delivery tracking |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`. They're also what makes the dedup quirks below
concrete: run against `snapshot-a` then `snapshot-b` and diff the output.

## Deduplication

We poll every 15 minutes, and feeds keep re-listing old entries on every
poll (there's no "give me only what's new" query any of the three providers
support), so most of what a run fetches has already been sent. `run_once`
(`digest.py`) filters each channel's matched items down to the ones that
haven't been delivered *to that channel* before, using a `dedup_key` that
`feeds.fetch` computes for every item and a `deliveries` table in
`store.py` that records `(channel, dedup_key)` once a send succeeds.

Two things worth knowing if you're touching this:

* **Why per-channel, not global.** The same item can match more than one
  channel's keywords. That's not a duplicate — it's two different audiences
  who both care. So "have we sent this" is scoped to `(channel, dedup_key)`,
  not just `dedup_key`.

* **Why `dedup_key` is a normalised link, not the provider's id.** Every
  provider we pull from makes raw ids useless for this on their own:
  `newsroom` entries are stable but their URL carries a `utm_campaign` tag
  that changes on every crawl; `generic` (wire) mints a new `guid` whenever
  an item is edited even though its link doesn't move; `blogroll` has no id
  at all. The one thing that holds still everywhere is the link once you
  strip tracking parameters, so that's what identity is built from. See the
  module docstring and `_normalize_link` in `feeds.py` for the details, and
  `fixtures/snapshot-b/*` for real examples of each quirk (look at how
  `newsroom.json`'s repeated entries change their `utm_campaign`, and
  `wire.json`'s repeated guid gets a `-r2` suffix, while both keep their
  link). The provider id is still stored as `raw_id` in the `items` archive
  table for manual lookups, it's just not what dedup is keyed on.

* **Failed sends don't get marked delivered.** `channels.send` raises on
  failure and `digest.py` only calls `store.mark_delivered` after `send`
  returns, so a down channel gets retried next tick instead of silently
  losing the item — consistent with the existing "best effort, cron retries"
  delivery contract described in `channels.py`.

If a feed ever starts giving you a genuinely stable, edit-surviving id,
prefer that over the link-based key for that format — the link heuristic is
a good general fallback, not a law of nature.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
