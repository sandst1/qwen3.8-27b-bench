# notify-digest

A small cron job that pulls a handful of feeds, filters the items per channel,
and sends each channel a digest.

## Running

```sh
cp config.example.toml config.toml
python3 digest.py --config config.toml --db digest.sqlite3
```

`--dry-run` prints what would be sent instead of sending it, and does not
consume anything — the next real run still delivers those items.

Requires Python 3.11+ (uses `tomllib`). No third-party dependencies.

## Layout

| File | Purpose |
| --- | --- |
| `digest.py` | CLI entry point, the per-run loop |
| `feeds.py` | Fetching, normalising, and **identifying** items from the three feed formats |
| `store.py` | SQLite: the item archive and the delivery ledger |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |
| `test_digest.py` | Regression tests, mostly about not sending things twice |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Not sending the same thing twice

This is the part that is easy to break, so it is worth reading before changing
`feeds.py` or `store.py`.

The feeds keep listing an item for as long as the provider feels like it, so
"what is in the feed" is not the same question as "what is new". The job keeps
a `deliveries` ledger of (channel, item key) pairs that have actually been
sent, and only sends what is missing from it.

That only works if an item's key is stable between polls, and each provider
mutates a different field:

| feed | identity comes from | because this churns between polls |
| --- | --- | --- |
| `newsroom` | `entry_id` | the url — `utm_campaign` rotates weekly |
| `blogroll` | permalink | title and excerpt, edited in place; there is no id |
| `wire` | link | the `guid`, regenerated on every edit |

So there is no single field that works for all three, which is why the rules
live per format in `feeds.py` — the long comment at the top of that module
explains each choice and what breaks if you "simplify" it. Switching any feed
to a different field is what caused the original bug.

Two other decisions worth knowing:

- **The ledger is per channel.** An item can match several channels, and a
  global "already sent" flag would let the first channel processed consume it
  and starve the rest.
- **Delivery is at-least-once.** The ledger is written only after a channel
  accepts the digest, so an outage means a repeat rather than a silent loss. If
  you ever swap that ordering, understand you are choosing "items sometimes
  disappear" over "items sometimes arrive twice".

Run the tests after touching any of this:

```sh
python3 -m unittest -v
```

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```

### Upgrading an existing box

The database schema is migrated automatically on connect, but a database from
before the dedupe fix has no delivery ledger, so the first run would treat
everything currently in the feeds as new and send it all one last time. Seed
the ledger first:

```sh
python3 digest.py --config config.toml --db digest.sqlite3 --seed
```

`--seed` marks everything currently in the feeds as already delivered without
sending it. Run it once, while cron is stopped, then start cron as usual. Only
items published after that point go out.

On a brand new install you can skip `--seed` if you want the current contents
of the feeds as a first digest, or use it if you would rather start quiet.
