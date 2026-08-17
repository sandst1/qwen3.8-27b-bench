"""SQLite persistence.

Two tables with two different jobs:

`items` is an archive of everything we have ever seen, mostly so that we can
answer "did this ever come through?" when someone asks. Nothing reads it from
code. It is now one row per distinct item rather than one row per sighting,
which is what makes the `first_seen` column mean what its name claims — before,
every poll inserted a fresh row, so the earliest timestamp recorded for an item
was really just whenever the most recent poll ran.

`deliveries` is the ledger that stops the job re-sending the same item every 15
minutes. It records the (channel, item key) pairs that have actually gone out.

Why the ledger is keyed per channel rather than globally: an item can match
several channels, and in the default config it does — the port fee story goes
to both "ops" and "everything". A single global "sent" flag would let whichever
channel is processed first consume the item and starve the rest.

Item keys are computed in feeds.py; see that module for why each feed derives
identity from a different field.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    key         TEXT,
    raw_id      TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

-- Partial index: rows archived before this fix have key IS NULL and are left
-- alone rather than merged on incomplete information.
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_key
    ON items(key) WHERE key IS NOT NULL;

CREATE TABLE IF NOT EXISTS deliveries (
    channel   TEXT NOT NULL,
    item_key  TEXT NOT NULL,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, item_key)
);
"""


def _migrate(conn):
    """Bring a pre-dedupe database up to the current schema.

    Databases created before this fix have an `items` table without the `key`
    column, and CREATE TABLE IF NOT EXISTS will not add it. Existing rows keep
    key = NULL: their keys cannot be recomputed after the fact because the feed
    format that produced each row is not recorded, and guessing risks
    suppressing a real item.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if columns and "key" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN key TEXT")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_items(conn, source, items):
    """Archive items. Re-seeing an item is a no-op, which preserves first_seen."""
    conn.executemany(
        "INSERT INTO items (source, key, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        # The WHERE restates the partial index's predicate; without it SQLite
        # cannot match the conflict target to idx_items_key.
        " ON CONFLICT(key) WHERE key IS NOT NULL DO NOTHING",
        [
            (
                source,
                i["key"],
                i.get("raw_id"),
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
    """Return the items not yet delivered to `channel`, order preserved.

    Also de-duplicates within the batch: a single poll can return the same item
    twice (a feed listing a story under two sections), and without this the
    digest would show it twice in one message.
    """
    if not items:
        return []

    keys = [i["key"] for i in items]
    placeholders = ",".join("?" * len(keys))
    already = {
        row["item_key"]
        for row in conn.execute(
            "SELECT item_key FROM deliveries"
            f" WHERE channel = ? AND item_key IN ({placeholders})",
            (channel, *keys),
        )
    }

    fresh, seen = [], set()
    for item in items:
        key = item["key"]
        if key in already or key in seen:
            continue
        seen.add(key)
        fresh.append(item)
    return fresh


def mark_sent(conn, channel, items):
    """Record items as delivered to `channel`.

    Call this only after delivery has actually succeeded — see the note on
    at-least-once delivery in digest.run_once.
    """
    conn.executemany(
        "INSERT INTO deliveries (channel, item_key) VALUES (?, ?)"
        " ON CONFLICT(channel, item_key) DO NOTHING",
        [(channel, i["key"]) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


def count_deliveries(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"]
