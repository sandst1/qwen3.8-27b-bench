"""SQLite persistence.

Two tables, with quite different jobs:

`sent` is the dedup ledger and the reason this job stopped spamming people.
One row per (channel, item) that has been *successfully delivered*. It is
the thing that makes the every-15-minutes cron idempotent.

`items` is an archive of everything we have ever seen, so that we can answer
"did this ever come through?" when someone asks. Nothing in the send path
reads it -- it is deliberately not the dedup source, see below.


Why the ledger is keyed per channel
-----------------------------------
Channel filters overlap by design: the shipped config has an "everything"
channel with no keywords alongside narrower ones, so every item matches at
least two channels. A single global "have we sent this?" flag would let
whichever channel happens to be processed first consume the item, and the
others would never see it. Keying on (channel, source, identity) keeps
channels independent of each other and of their ordering in the config.

`source` is part of the key because identities are only meaningful within
the feed that issued them ("id:84121" means nothing outside newsroom). Two
providers covering the same story are two items and both get sent; clustering
stories across providers is a different feature and is not attempted here.


Why the archive is not the dedup source
---------------------------------------
The archive records what we *saw*; the ledger records what we *delivered*.
They diverge whenever a channel is down, a keyword filter does not match, or
a new channel is added later. Deduping against "have we seen it" would drop
notifications that were never actually sent to anyone.
"""

import sqlite3

# Keep SQLite's parameter limit at arm's length; older builds cap host
# parameters at 999 per statement and a large feed would otherwise blow up.
_CHUNK = 400

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    raw_id      TEXT,
    identity    TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

-- Partial index: rows archived before identities existed have identity NULL
-- and are excluded rather than colliding with each other.
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_identity
    ON items(source, identity) WHERE identity IS NOT NULL;

CREATE TABLE IF NOT EXISTS sent (
    channel   TEXT NOT NULL,
    source    TEXT NOT NULL,
    identity  TEXT NOT NULL,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, identity)
) WITHOUT ROWID;
"""


def _migrate(conn):
    """Bring a pre-existing digest.sqlite3 up to the current schema.

    The utility box has a live database from before identities existed, so
    the columns are added in place. Old rows keep identity NULL: we cannot
    reconstruct it (we no longer know which format produced them), and
    guessing would risk suppressing a live notification. They stay as
    historical records and the partial unique index ignores them.

    Those old rows include the duplicates this bug produced -- one copy per
    15-minute tick. They are left alone rather than deleted; deduplicating
    someone's archive without being asked is not this function's call. To
    clean them up by hand:

        DELETE FROM items WHERE identity IS NULL AND id NOT IN (
            SELECT MIN(id) FROM items WHERE identity IS NULL
            GROUP BY source, title, link);
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if not existing:
        return  # Fresh database; SCHEMA already created everything.
    # ALTER TABLE ADD COLUMN cannot take a non-constant default, so these are
    # added nullable and backfilled below.
    if "identity" not in existing:
        conn.execute("ALTER TABLE items ADD COLUMN identity TEXT")
    if "last_seen" not in existing:
        conn.execute("ALTER TABLE items ADD COLUMN last_seen TEXT")
        conn.execute("UPDATE items SET last_seen = first_seen WHERE last_seen IS NULL")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # The ledger must survive a box that loses power between the webhook POST
    # and the commit; the alternative is re-notifying everyone on reboot.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_items(conn, items):
    """Upsert into the archive, one row per distinct (source, identity).

    Previously this INSERTed unconditionally, so a single item accumulated
    ~96 rows a day and the archive could not actually answer the question it
    exists for. Re-seeing an item now refreshes the mutable fields and bumps
    last_seen, which is also how you spot an item that was edited after we
    delivered it.
    """
    if not items:
        return
    conn.executemany(
        "INSERT INTO items (source, raw_id, identity, title, link, summary,"
        "                   published, last_seen)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))"
        " ON CONFLICT(source, identity) WHERE identity IS NOT NULL"
        " DO UPDATE SET raw_id    = excluded.raw_id,"
        "               title     = excluded.title,"
        "               link      = excluded.link,"
        "               summary   = excluded.summary,"
        "               published = excluded.published,"
        "               last_seen = excluded.last_seen",
        [
            (
                i["source"],
                i["raw_id"],
                i["identity"],
                i["title"],
                i["link"],
                i.get("summary"),
                i.get("published"),
            )
            for i in items
        ],
    )
    conn.commit()


def unsent(conn, channel, items):
    """Return the items this channel has not already been delivered.

    Input order is preserved (callers have already sorted by recency), and
    repeats within a single poll are collapsed so that a provider listing the
    same entry twice does not produce it twice in one digest.
    """
    if not items:
        return []

    keys = [(i["source"], i["identity"]) for i in items]
    already = set()
    for start in range(0, len(keys), _CHUNK):
        chunk = keys[start : start + _CHUNK]
        placeholders = ",".join(["(?, ?)"] * len(chunk))
        params = [channel]
        for source, identity in chunk:
            params.extend((source, identity))
        rows = conn.execute(
            "SELECT source, identity FROM sent"
            f" WHERE channel = ? AND (source, identity) IN ({placeholders})",
            params,
        )
        already.update((r["source"], r["identity"]) for r in rows)

    fresh = []
    seen_in_batch = set()
    for item, key in zip(items, keys):
        if key in already or key in seen_in_batch:
            continue
        seen_in_batch.add(key)
        fresh.append(item)
    return fresh


def mark_sent(conn, channel, items):
    """Record that `channel` has been delivered `items`.

    Call this only *after* delivery succeeds. Marking first would mean a
    webhook outage silently ate the digest: the items would be flagged as
    delivered and never retried. Marking afterwards means a crash between
    the POST and this commit re-sends next tick -- at-least-once, which for
    a notification digest is the right side to fail on.
    """
    if not items:
        return
    conn.executemany(
        "INSERT INTO sent (channel, source, identity) VALUES (?, ?, ?)"
        " ON CONFLICT(channel, source, identity) DO NOTHING",
        [(channel, i["source"], i["identity"]) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


def count_sent(conn, channel=None):
    if channel is None:
        return conn.execute("SELECT COUNT(*) AS n FROM sent").fetchone()["n"]
    return conn.execute(
        "SELECT COUNT(*) AS n FROM sent WHERE channel = ?", (channel,)
    ).fetchone()["n"]
