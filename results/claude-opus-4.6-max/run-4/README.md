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
| `feeds.py` | Fetching and normalising the three feed formats |
| `store.py` | SQLite archive of everything seen |
| `render.py` | Digest text |
| `channels.py` | Delivery (stdout, webhook) |

The `fixtures/` directory holds two snapshots of the feeds taken at different
times, so you can work on this without network access. Point the config at
`snapshot-a` or `snapshot-b`.

## Deployment

Runs from cron on the utility box:

```
*/15 * * * * cd /srv/notify-digest && python3 digest.py >> /var/log/digest.log 2>&1
```

## Deduplication

Each item is sent to a channel at most once. The `sent` table in the SQLite
database records `(channel, link)` pairs. On each run, only items whose link
has not yet been delivered to that channel are included in the digest.

**Why `link` as the dedup key?** We evaluated all available identifiers:

| Format | Candidate ID | Problem |
| --- | --- | --- |
| newsroom | `entry_id` | Stable, but other formats don't have one |
| blogroll | *(none)* | No machine ID at all; permalink is the only anchor |
| wire/generic | `guid` | Regenerated on every edit (typos, retitles) |

The item `link` (permalink/URL) is stable across all three providers and
uniquely identifies a story. If a provider ever serves two different stories
at the same URL we would miss the second, but in practice they don't.

**Consequence**: if you need to re-send an item (e.g. after a bad render),
delete its row from the `sent` table:

```sql
DELETE FROM sent WHERE channel = 'ops' AND link = 'https://...';
```

The next cron tick will re-deliver it.
