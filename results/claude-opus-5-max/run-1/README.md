# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

| Flag | Effect |
| --- | --- |
| `--dry-run` | Print what would be sent. Writes nothing to the database. |
| `--mark-seen` | Record everything currently in the feeds as already delivered, without sending it. |

Exits non-zero if any feed or channel failed, so cron/monitoring notices.
Nothing that failed is marked as delivered, so it retries on the next tick.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

```sh
python3 -m unittest discover      # 30 tests, no network
```

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching, normalising the three feed formats, **deciding item identity** |
| `store.py` | SQLite: the `sent` dedup ledger and the `items` archive |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |
| `test_digest.py` | Dedup behaviour, driven off the fixtures |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

---

## How repeats are suppressed

Cron runs this every 15 minutes, but the providers keep an item in their feed
for days. So **every run re-fetches things we already sent** — that is normal
and not something we can ask the providers to change. Suppressing the repeats
is this job's responsibility.

Each run does:

```
fetch -> filter by channel keywords -> drop what this channel already got
      -> render -> deliver -> only then record it as delivered
```

The ledger is the `sent` table: one row per `(channel, source, identity)` that
has been **successfully delivered**.

### What counts as "the same item"

This is the whole problem, and there is no single field that answers it,
because each of the three providers breaks a different obvious choice. From
the two fixture snapshots (the same feeds, a few hours apart):

| Feed | Stable across polls | Churns across polls | So the naive key that breaks is |
| --- | --- | --- | --- |
| `newsroom` | `entry_id`, title, summary | the **URL** — `utm_campaign` goes `w33` → `w34` | dedup by link |
| `blogroll` | permalink | **title** and **excerpt** — edited in place, retitled "… (updated)" | dedup by content hash |
| `wire` | link, title | **guid** — regenerated on every edit, `…-0031` → `…-0031-r2` | dedup by `raw_id`/guid |

Note `blogroll` has no identifier of any kind, and `wire`'s identifier is
actively misleading. Picking any one rule globally re-notifies at least one
feed. So identity is chosen **per format**, in `feeds.py`, next to the quirk
that motivates it:

- **`newsroom` → `id:<entry_id>`.** The publisher's id is stable under both
  edits and the weekly campaign rotation, so it beats any URL heuristic.
- **`blogroll` → `url:<normalised permalink>`.** Nothing else exists, and the
  permalink survives the retitles.
- **`wire` → `url:<normalised link>`.** The guid is known-unstable; ignore it.

**Adding a fourth provider?** Prefer a publisher-assigned id *only if it is
stable under edits*; otherwise use the normalised link. Never fold
title/summary into identity — that turns every typo fix into a re-notification,
which is the exact complaint this design exists to stop.

Identities are stored as readable strings rather than opaque hashes, so the
ledger can be read directly when someone asks why something did or didn't go
out:

```sh
sqlite3 digest.sqlite3 'SELECT * FROM sent ORDER BY sent_at DESC LIMIT 20;'
```

### URL normalisation

Only for comparison — the link in the delivered digest is always the
publisher's original, untouched. Normalising strips campaign parameters
(`utm_*`, `fbclid`, `gclid`, …) and the fragment, lowercases the scheme and
host (but **not** the path, which is case-sensitive per RFC 3986), drops a
default port and a trailing slash, and sorts the surviving query.

The strip-list is deliberately short. Wrongly stripping a *meaningful*
parameter merges two distinct items and silently drops a notification, which
is worse than the duplicate it would have saved.

---

## Decisions worth knowing about

**The ledger is per channel, not global.** Channel filters overlap by design —
the shipped config has an `everything` channel with no keywords alongside
narrower ones, so every item matches at least two channels. A single global
"already sent?" flag would let whichever channel is processed first consume the
item and the rest would never see it. Keying on channel also makes the outcome
independent of channel order in the config.

**Items are marked sent *after* delivery succeeds, never before.** If the
webhook is down, the items stay unrecorded and go out on the next tick. The
cost is that a crash between the POST and the commit re-sends — at-least-once.
For a notification digest that is the right side to fail on: a duplicate is an
annoyance, a silent miss is a bug you never find out about.

**Edits do not re-notify.** An item that is corrected or retitled after we sent
it stays quiet; the archive is updated so the current text is still
queryable. Given the complaint was repetition, and `feeds.py` notes the
providers edit for typos and tags, re-notifying on edit would reintroduce the
original problem. If you ever *do* want "significantly updated" to re-notify,
that is a deliberate feature — add a separate signal for it rather than
weakening identity.

**One bad channel doesn't block the others.** A `DeliveryError` is logged, that
channel is skipped, the rest are still delivered, and the run exits non-zero.

**Stories are not clustered across providers.** `newsroom`'s "Regulator opens
inquiry into port fees" and `wire`'s "Port fee inquiry opened" are two items
from two outlets and both get sent. Cross-provider clustering is a different,
much fuzzier feature and is not attempted here.

**`--dry-run` writes nothing at all.** Previously it skipped delivery but still
wrote to the database. Now that state controls whether people get notified, a
dry run that marked things as seen would silently suppress real alerts.

---

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```

### Upgrading an existing box

The schema migrates itself on connect (adds `identity` and `last_seen` to
`items`, creates `sent`). Rows archived before this change keep `identity NULL`
— it can't be reconstructed, and guessing risked suppressing a live
notification, so they're left as historical records.

Those old rows include the duplicates this bug produced, one per item per tick.
They're left alone rather than auto-deleted; `store._migrate` carries the
`DELETE` if you want to tidy them by hand.

The ledger starts empty, so **the first run would send the current feed
contents once more.** To avoid that final burst, adopt the current window
silently:

```sh
python3 digest.py --config config.toml --db digest.sqlite3 --mark-seen
```

The same command is how you add a channel without back-filling it with
everything currently in the feeds.

### Known limits

- The `sent` table grows without bound. At this volume (a few items a day)
  that's harmless for years; if a feed ever gets big, add a reaper that drops
  rows older than the longest provider retention window — **not** shorter, or
  items will re-notify when they age out of the ledger while still in the feed.
- A brand-new channel receives everything currently in the feeds on its first
  run. That's usually what you want; use `--mark-seen` when it isn't.
- Delivery is at-least-once, not exactly-once. See above.
