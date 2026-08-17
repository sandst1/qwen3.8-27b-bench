"""SQLite persistence.

The `items` table is an archive of everything we have ever seen.
The `sent_items` table tracks which (raw_id, channel) pairs have already been
dispatched, so that cron re-runs never send duplicates.
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

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

CREATE TABLE IF NOT EXISTS sent_items (
    raw_id      TEXT NOT NULL,
    channel     TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (raw_id, channel)
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
        "INSERT INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in items
        ],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


def is_sent(conn, raw_id, channel):
    """Return True if this item was already sent to this channel."""
    row = conn.execute(
        "SELECT 1 FROM sent_items WHERE raw_id = ? AND channel = ?",
        (raw_id, channel),
    ).fetchone()
    return row is not None


def mark_sent(conn, raw_ids, channel):
    """Record that these items have been sent to a channel."""
    conn.executemany(
        "INSERT OR IGNORE INTO sent_items (raw_id, channel) VALUES (?, ?)",
        [(rid, channel) for rid in raw_ids],
    )
    conn.commit()
