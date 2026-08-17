"""SQLite persistence.

Two tables, with different jobs:

`items` is an archive of everything we have ever seen, so that we can answer
"did this ever come through?" when someone asks.  Nothing reads it
automatically.

`deliveries` is the state that stops us re-sending.  It records one row per
(channel, item) that has actually gone out, and it is the reason the cron
job no longer repeats itself every 15 minutes.

Why the key is (channel, item) and not just (item)
--------------------------------------------------
Channels have overlapping keyword filters -- in the sample config an item
about port fees matches both "ops" and the "everything" firehose.  A single
global "sent" flag would let whichever channel was processed first consume
the item and starve the rest.  Per channel state costs one small row each
and makes the channel loop order irrelevant.

The cost of that choice: a channel added to the config later has no history,
so it receives everything currently in the feeds as its first digest.  That
is usually what you want from a new subscription, and it is bounded by how
much the providers keep in their feed.  `digest.py --mark-seen` is there for
when it is not.

`deliveries` is never pruned.  It grows by a handful of rows per run, which
is nothing at this cadence, and any retention window would re-notify people
about items that outlive it -- exactly the bug we are fixing.
"""

import sqlite3

import identity

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    raw_id      TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    item_key    TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

CREATE TABLE IF NOT EXISTS deliveries (
    channel   TEXT NOT NULL,
    item_key  TEXT NOT NULL,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, item_key)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    """Bring a database created before delivery tracking up to date.

    Only `items` needs it; `deliveries` is created by CREATE TABLE IF NOT
    EXISTS above.  Existing rows keep item_key NULL rather than being
    backfilled: the archive is only ever read by hand, and backfilling
    cannot be done correctly anyway for rows whose links were archived
    before canonicalisation existed.

    The index on item_key is created here rather than in SCHEMA because
    SCHEMA does not run against a pre-existing `items` table -- CREATE TABLE
    IF NOT EXISTS skips it -- so on a live database the column only exists
    once the ALTER below has run.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "item_key" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN item_key TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_key ON items(item_key)")


def record_items(conn, source, items):
    """Archive items we have not archived before.

    Inserting unconditionally (the previous behaviour) added a fresh copy of
    every item on every run, four times an hour, which made `first_seen`
    meaningless and the table impossible to read.  There is no UNIQUE
    constraint to lean on here because live databases already contain those
    duplicates, so the guard is a WHERE NOT EXISTS instead.
    """
    rows = []
    for item in items:
        key = identity.item_key(item)
        rows.append(
            (
                source,
                item["raw_id"],
                item["title"],
                item["link"],
                item.get("summary"),
                item.get("published"),
                key,
                key,  # again, for the WHERE NOT EXISTS guard
            )
        )

    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published, item_key)"
        " SELECT ?, ?, ?, ?, ?, ?, ?"
        " WHERE NOT EXISTS (SELECT 1 FROM items WHERE item_key = ?)",
        rows,
    )
    conn.commit()


def unsent(conn, channel, items):
    """Return the items in `items` that `channel` has not been sent yet.

    Input order is preserved.  Items that share a key within a single batch
    are collapsed, since two providers can carry the same story in one run
    and the reader should see it once.
    """
    batch = []
    seen = set()
    for item in items:
        key = identity.item_key(item)
        if key in seen:
            continue
        seen.add(key)
        batch.append((key, item))

    if not batch:
        return []

    # Queried against this run's keys rather than the channel's whole
    # history, so the work stays proportional to the feed, not to how long
    # the job has been running.
    placeholders = ",".join("?" * len(batch))
    delivered = {
        row["item_key"]
        for row in conn.execute(
            "SELECT item_key FROM deliveries"
            f" WHERE channel = ? AND item_key IN ({placeholders})",
            [channel, *(key for key, _ in batch)],
        )
    }
    return [item for key, item in batch if key not in delivered]


def mark_sent(conn, channel, items):
    """Record that `channel` has been sent `items`.

    Call this only after delivery succeeds.  Marking first would mean a
    channel outage silently ate the items it failed to deliver.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO deliveries (channel, item_key) VALUES (?, ?)",
        [(channel, identity.item_key(i)) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
