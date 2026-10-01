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

Tests: `python3 -m unittest test_digest` (runs offline against `fixtures/`).

## No repeats: how "new" is decided

Feeds serve a rolling window of recent items, and every 15-minute cron
tick refetches that whole window. To stop readers getting the same
stories over and over, each run sends a channel only the items that
*that channel* has not received before.

That dedup state lives in the `delivered` ledger in the SQLite file
(`--db`, default `digest.sqlite3`), keyed on `(channel, URL)` where the
URL is stripped of its query and fragment. Two choices, both forced by
the providers (see `store.py` and `fixtures/` for concrete examples):

* **URL, not feed item id** — blogroll carries no ids, wire regenerates
  its guid whenever a story is edited, and newsroom rotates `utm_*`
  tags on the same story's URL. A retitled story is still the same
  story to someone who already read it; origin+path survives all three.
  (Caveat: a provider identifying distinct stories by query string
  would be collapsed to one item — none of ours does that.)
* **Per channel, not global** — "already seen" is a fact about an
  audience, not about the robot. #ops having seen a story does not make
  it old to #firehose, which is why the ledger is not one global list.

If a send fails, the run aborts before anything is recorded for that
channel, so the next tick retries the undelivered items. To replay
history — e.g. after adding a new channel — delete the db file.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite `delivered` ledger: what each channel already got |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |
| `test_digest.py` | Regression tests for the no-repeat rules |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```
