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
| `feeds.py` | Fetching and normalising the three feed formats, **and deciding item identity** |
| `store.py` | SQLite archive of everything seen, plus the sent ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`. Running `snapshot-a`, then `snapshot-a` again,
then `snapshot-b` is the quickest way to check de-duplication still works:
the second run should be silent and the third should deliver exactly one item
("Union responds to port fee inquiry").

## De-duplication

The job runs every 15 minutes and feeds keep returning the same items, so
something has to decide "have we sent this before?". That decision is an
identity (`dedup_key`) computed in `feeds.py`, and a `sent` ledger in
`store.py` keyed on **(channel, dedup_key)**.

There is no single field that is stable across all three providers, so each
format gets its own rule:

| format | `raw_id` across polls | `link` across polls | we key on |
| --- | --- | --- | --- |
| `newsroom` | stable (`entry_id`) | rotates (weekly `utm_campaign`) | `raw_id` |
| `blogroll` | absent entirely | stable (permalink) | `link` |
| `generic` | regenerated on every edit | stable | `link` |

The two traps, because they cost real time to rediscover:

* **newsroom** links carry a `utm_campaign` that rolls over weekly, so the
  same article reappears under a new URL. Keying it on the link re-sends
  every article once a week.
* **generic** (the wire) has a `guid` that looks authoritative but the
  provider regenerates it on any edit (`…0031` becomes `…0031-r2`). Keying
  it on the guid re-sends an item every time someone fixes a typo.

Titles and summaries are unusable as identity — both blogroll and the wire
edit them in place, so any content hash changes while the item stays the
same. Links are stripped of tracking parameters before use as identity; the
item keeps its original link for display.

Decisions worth knowing before you change this:

* **Keyed per channel.** One item can match several channels, and marking it
  sent for `ops` must not silence it for the firehose.
* **Keyed per source.** If two providers carry the same story, both are
  still delivered — same as before. Cross-source de-duplication is a
  separate feature and a judgement call about which copy wins.
* **Identity, not content.** An item edited upstream after we sent it is
  *not* sent again. Re-announcing every typo fix is the noise this was
  meant to remove, but a materially rewritten post will stay silent. Change
  that in `feeds.py`, not in the ledger.
* **Send first, record after.** A channel that fails records nothing and is
  retried on the next tick; one broken channel no longer blocks the others.
  A crash between sending and the commit re-sends that digest, which is the
  safe direction to fail in.
* **The ledger is never pruned.** At current volumes it stays small for
  years, and any retention window would re-send anything that outlived it.

A channel with nothing new is now skipped entirely rather than sent the same
digest again, so a quiet run prints `sent 0 items` and delivers nothing.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```

The `sent` ledger lives in the SQLite database, so **deleting or moving
`digest.sqlite3` makes every current feed item look new** and the next run
will send a full backlog to every channel. Exit status is non-zero if any
channel failed to deliver.
