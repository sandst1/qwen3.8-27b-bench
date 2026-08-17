"""SQLite persistence.

The `items` table is an archive of everything we have ever seen.

The `sent_items` table tracks which (item, channel) pairs have already been
delivered.  This is how we avoid sending duplicates across cron runs.

Deduplication key: (source, raw_id) uniquely identifies a feed item.  We use
a UNIQUE constraint so that `record_items` silently skips items we've already
archived (INSERT OR IGNORE).
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    raw_id      TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_raw_id
    ON items(source, raw_id);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

CREATE TABLE IF NOT EXISTS sent_items (
    source      TEXT NOT NULL,
    raw_id      TEXT NOT NULL,
    channel     TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, raw_id, channel)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_items(conn, source, items):
    conn.executemany(
        "INSERT OR IGNORE INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in items
        ],
    )
    conn.commit()


def is_sent(conn, source, raw_id, channel):
    """Return True if this item was already sent to this channel."""
    row = conn.execute(
        "SELECT 1 FROM sent_items WHERE source=? AND raw_id=? AND channel=?",
        (source, raw_id, channel),
    ).fetchone()
    return row is not None


def mark_sent(conn, source, raw_id, channel):
    """Record that an item was delivered to a channel."""
    conn.execute(
        "INSERT OR IGNORE INTO sent_items (source, raw_id, channel) VALUES (?, ?, ?)",
        (source, raw_id, channel),
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
