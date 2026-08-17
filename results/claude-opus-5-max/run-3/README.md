# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

Each channel is sent an item **once**. See "How repeats are avoided" below,
which is the only non-obvious thing in here.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

| Flag | Effect |
| --- | --- |
| `--dry-run` | Print what would be sent. Records nothing, so a real run afterwards still sends it. |
| `--mark-seen` | Record everything currently in the feeds as already delivered, without sending. For adopting a feed or channel that should start from now rather than receive the backlog. |

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

```sh
python3 -m unittest          # the test suite runs entirely off fixtures/
```

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `identity.py` | Deciding when two feed entries are the same item |
| `store.py` | SQLite archive, and the record of what has been delivered |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |
| `test_digest.py` | Tests, mostly about not repeating yourself |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## How repeats are avoided

Feeds republish their entire window on every poll, so at a 15 minute cadence
the job sees the same items ~96 times a day. It used to send them all every
time. Now `store.deliveries` holds one row per `(channel, item)` that has
actually gone out, and a channel's digest only ever contains items with no
such row.

### What counts as "the same item"

This is the whole problem, because **each provider breaks a different obvious
identifier** — and the two fixture snapshots demonstrate all three:

| Provider | `raw_id` | `link` |
| --- | --- | --- |
| newsroom | stable `entry_id` | churns — `?utm_campaign=w33` becomes `w34` each week |
| blogroll | absent, always `None` | stable permalink |
| wire | churns — gains `-r2` on every edit | stable |

So `raw_id` works for one feed, `link` works for two, and hashing the title or
body works for none (blogroll retitled a post to "... (updated)"; wire and
blogroll both edited body text between the snapshots).

**The key is the link with tracking parameters stripped.** Canonicalising the
link repairs the one case `link` gets wrong, which makes it work for all three.
`identity.py` has the details and the reasoning.

`raw_id` is deliberately not consulted even for newsroom, where it is the
better identifier. "Use `raw_id` for this provider and the link for that one"
is a per-provider rule, and a per-provider rule is one that the next provider
added to the config gets wrong *silently*. One uniform rule fails in one place,
loudly, and there is a test for it.

Two consequences worth knowing before you change anything:

- **An edit is not a new item.** A corrected table or an added paragraph reuses
  the key and is not re-sent. That was the point — people complained about
  repeats, not about missing errata. If you ever want errata to re-notify, that
  is a new feature, not a bug fix, and it needs a rule for which edits matter.
- **Canonicalisation is deliberately conservative.** It strips `utm_*` and a
  short list of known click IDs, and nothing else. Query parameters like `?id=`
  are kept, because a *false merge* silently suppresses an item, which is much
  worse and much harder to notice than a duplicate.

### Deliberate choices in the run loop

- Delivery state is per `(channel, item)`, not per item. Channel filters
  overlap — in the sample config a port-fee item matches both `ops` and the
  `everything` firehose — and a single global flag would let whichever channel
  ran first swallow the item.
- Items are marked **after** the send succeeds. Marking first would mean a
  channel outage silently ate whatever it failed to deliver.
- A failing channel is logged and skipped rather than aborting the run, so one
  broken webhook does not delay every other channel by 15 minutes. Its items
  stay unmarked and go out on the next tick.
- A channel added to the config later has no history, so its first digest is
  everything currently in the feeds. That is usually what you want from a new
  subscription; use `--mark-seen` when it is not.
- `deliveries` is never pruned. It grows by a few rows per run, which is
  nothing at this cadence, and any retention window would re-notify people
  about items that outlive it — precisely the bug being fixed here.

### Note on the archive

`items` is still the "did this ever come through?" archive and nothing reads it
automatically. It used to gain a fresh copy of every item on every run, which
made its `first_seen` column meaningless; it now records each item once.
Existing databases keep their duplicate rows and are migrated in place by
adding a nullable `item_key` column — old rows are not backfilled, since links
archived before canonicalisation cannot be keyed reliably.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```

The first run after deploying this change sends one last digest of whatever is
currently in the feeds — the `deliveries` table starts empty and cannot know
what the old code already sent — and is quiet from then on. To skip that, prime
it once before the next tick:

```sh
cd /srv/notify-digest && python3 digest.py --mark-seen
```
