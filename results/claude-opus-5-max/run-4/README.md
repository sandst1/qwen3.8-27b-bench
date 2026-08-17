# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it, and writes
nothing, so you can run it as often as you like.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.
Tests: `python3 -m unittest`.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `identity.py` | What makes two entries "the same item" |
| `store.py` | SQLite archive + the delivery ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |
| `test_digest.py` | Tests, mostly regression tests for dedup |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Not sending the same thing twice

This is the part of the job that has actual subtlety in it, so it is worth
reading before changing anything near it.

Cron polls every 15 minutes; feeds hold an item for hours or days. So we see
every item around a hundred times and must send it once per channel. There
used to be no check for this at all — `store.py` archived everything seen but
nothing read the archive back, so every poll resent the entire feed. That is
the bug this is built to fix.

### Identity: the link, with tracking parameters stripped

Deciding what is "the same item" is the whole problem, because each provider
breaks a different obvious answer:

| provider | stable id? | stable link? | stable text? |
| --- | --- | --- | --- |
| `newsroom` | yes (`entry_id`) | **no** — `utm_*` rotate weekly | yes |
| `blogroll` | **no** — no id field exists | yes (permalink) | **no** — posts get edited |
| `wire` | **no** — regenerated on every edit | yes | **no** — posts get edited |

The two fixture snapshots are one cron tick apart and contain exactly one new
item. They also contain a trap for each of the three naive keys: keying on
`link` resends both newsroom items (the campaign tag rolled `w33`→`w34`),
keying on `raw_id` resends the wire item (`guid` gained an `-r2` suffix for a
copy edit), and keying on a hash of the text resends the blogroll and wire
items (both were edited in place). `blogroll` has no id at all, so a key built
from a missing id gives every post the *same* identity and silently suppresses
the whole feed after the first post — the worst outcome of the four, because
it looks like everything is working.

The one signal that survives all three is the link once tracking parameters
are removed. `identity.py` builds that key, plus a source-namespaced id key
when the provider gives one, and an item counts as already-delivered if *any*
of its keys has been. Extra keys can only ever make us suppress more, never
less, so an unreliable id is safe to carry: at worst it never helps, at best
it catches an item whose URL moved.

Keys are also recorded for items we *suppress*, not just ones we send, so
identities accumulate aliases. If wire rotates a guid one week and moves the
URL the next, the first poll has already linked the new guid to the known
link, and the second poll still recognises the item.

### Edits deliberately do not re-notify

An item edited in place is the same item and stays silent — a retitle or a
typo fix is not news, and "the same thing keeps arriving" is the complaint we
are fixing. If a channel ever genuinely needs edit notifications, that is a
new opt-in feature, not a change to the identity key: folding the body text
into identity would resend every blogroll and wire copy edit to everyone.
`test_edited_title_and_body_are_not_a_new_item` is the test to revisit.

### The ledger is per channel, not global

`deliveries` is keyed `(channel, identity)`. "Have we seen this?" is the wrong
question; "have we sent this *to this channel*?" is the right one. A global
seen-set breaks two ordinary cases: two channels matching one item (with the
shipped config a port-fee story matches both `ops` and the `everything`
firehose, and only the first listed would get it), and a channel that is added
or has its keywords widened later, which would start off deaf.

This makes a channel's `name` load-bearing. Two names that collide are
rejected at startup, because they would share a ledger and the second channel
would go quiet for no visible reason. **Renaming a channel** cannot be
detected — the new name has no history, so it re-sends everything currently on
the feeds once. Run `--seed` after a rename if that matters.

### Items are recorded only after the channel accepts them

A failed send leaves the item unmarked, so it goes out on the next tick.
Marking before sending would turn every delivery blip into permanent silent
data loss, which is a worse bug than the duplicates.

For the same reason a failing channel no longer aborts the run: it is logged,
the other channels still get their digests, and the exit code is non-zero so
cron surfaces it. Previously one unreachable webhook silenced every channel
configured after it, for as long as the outage lasted.

### Retention is keyed on last-seen, not on delivery date

`--retention-days` (default 90) drops ledger entries, but on `last_seen`,
which is refreshed on every poll where the item is still on the feed. So an
entry can only be dropped once the item has been *gone* for the full window.
Pruning on the delivery date instead would expire long-lived items while they
were still being published and resend them — reintroducing this same bug on a
90-day delay, in a form that would be miserable to reproduce. Pass
`--retention-days 0` to keep everything.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```

**Upgrading an existing install:** the ledger starts empty, so the first poll
after deploying would treat everything currently on the feeds as new and fire
one last duplicate digest at everybody. Run once to adopt the current feed
contents silently first:

```sh
python3 digest.py --config config.toml --db digest.sqlite3 --seed
```

The existing `digest.sqlite3` is migrated in place on first connect (one added
nullable column; the archive is left alone).

### Exit codes

| code | meaning |
| --- | --- |
| 0 | ran; anything sent was accepted |
| 1 | at least one channel refused. Those items are **not** lost — they are unmarked and go out next tick. Worth alerting on if it persists. |
| 2 | bad config; nothing ran |

A feed that fails to load is a warning on stderr, not a failure, which is the
behaviour this has always had.

## Known wart

`items` is append-only by design — an archive of every *sighting*, so someone
can answer "did this ever come through?". That means several hundred rows a
day for the shipped config (six items × 96 polls), nearly all of them repeats
of the same handful of items. It is not in the read path of anything, so it
was left alone rather than rebuilt during a bugfix. If it ever gets big enough
to care about, the fix is a `UNIQUE(source, ident)` with an upsert and a
`times_seen` counter, which needs a table rebuild.
