"""SQLite persistence — deduplication and send tracking.

Design decisions (2026-08-16):
- `items` uses a UNIQUE constraint on (source, raw_id) so we never insert the
  same feed entry twice, even if cron fires repeatedly.
- `sent_items` tracks which (item, channel) pairs have already been delivered,
  so each channel only receives a given item once.
- We use INSERT OR IGNORE for items to make repeated fetches idempotent.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    raw_id      TEXT NOT NULL,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source, raw_id)
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

CREATE TABLE IF NOT EXISTS sent_items (
    item_id     INTEGER NOT NULL REFERENCES items(id),
    channel     TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (item_id, channel)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_items(conn, source, items):
    """Insert items we haven't seen before (duplicates are silently ignored)."""
    conn.executemany(
        "INSERT OR IGNORE INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in items
        ],
    )
    conn.commit()


def unsent_items_for_channel(conn, channel):
    """Return items that have never been sent to `channel`, oldest first."""
    rows = conn.execute(
        """
        SELECT i.id, i.source, i.raw_id, i.title, i.link, i.summary, i.published
        FROM items i
        WHERE i.id NOT IN (
            SELECT s.item_id FROM sent_items s WHERE s.channel = ?
        )
        ORDER BY i.published ASC, i.first_seen ASC
        """,
        (channel,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_sent(conn, item_ids, channel):
    """Record that these items have been delivered to `channel`."""
    conn.executemany(
        "INSERT OR IGNORE INTO sent_items (item_id, channel) VALUES (?, ?)",
        [(item_id, channel) for item_id in item_ids],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
