"""SQLite persistence.

The `items` table is an archive of everything we have ever seen, mostly so
that we can answer "did this ever come through?" when someone asks.

The `sent_items` table tracks which items have been delivered to which
channel, so we never send the same item twice.

Deduplication strategy
----------------------
An item is considered "already sent" to a channel if a row in sent_items
matches on EITHER of two signals:

  1. (channel, source, raw_id)  — when raw_id is not NULL
  2. (channel, source, link)

Why both?  Our three feed types have different stability properties:

  - newsroom: raw_id (entry_id) is stable, but the link gets new utm_*
    params each week.  → raw_id catches the duplicate.
  - wire: the guid (raw_id) is regenerated on every edit, but the link
    is stable.  → link catches the duplicate.
  - blogroll: has no raw_id at all; permalink (link) is stable.
    → link catches the duplicate.

Checking both means we handle all three without feed-specific logic in the
dedup path.
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
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    channel  TEXT NOT NULL,
    source   TEXT NOT NULL,
    raw_id   TEXT,
    link     TEXT NOT NULL,
    sent_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sent_channel_link
    ON sent_items(channel, source, link);
CREATE INDEX IF NOT EXISTS idx_sent_channel_rawid
    ON sent_items(channel, source, raw_id);
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


def is_already_sent(conn, channel, item):
    """Return True if this item was previously delivered to this channel."""
    # Check by link (universally available and stable for wire + blogroll).
    row = conn.execute(
        "SELECT 1 FROM sent_items WHERE channel = ? AND source = ? AND link = ?",
        (channel, item["source"], item["link"]),
    ).fetchone()
    if row:
        return True
    # Check by raw_id (stable for newsroom; covers the case where the link
    # gains new tracking params between runs).
    if item.get("raw_id"):
        row = conn.execute(
            "SELECT 1 FROM sent_items"
            " WHERE channel = ? AND source = ? AND raw_id = ?",
            (channel, item["source"], item["raw_id"]),
        ).fetchone()
        if row:
            return True
    return False


def mark_sent(conn, channel, items):
    """Record that these items were delivered to the given channel."""
    conn.executemany(
        "INSERT INTO sent_items (channel, source, raw_id, link)"
        " VALUES (?, ?, ?, ?)",
        [(channel, i["source"], i["raw_id"], i["link"]) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
