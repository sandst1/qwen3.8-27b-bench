"""SQLite persistence.

Two tables:

- `items` is an archive of everything we have ever seen, mostly so that we
  can answer "did this ever come through?" when someone asks.
- `deliveries` is the dedup ledger: one row per (channel, item_key) we have
  successfully delivered. digest.py consults it before sending, so an item
  reaches a channel at most once. Item keys come from feeds.item_key();
  dedup is per channel, because two channels can legitimately want the
  same story.
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


def delivered_keys(conn, channel):
    """The set of item keys already delivered to this channel."""
    rows = conn.execute(
        "SELECT item_key FROM deliveries WHERE channel = ?", (channel,)
    )
    return {row["item_key"] for row in rows}


def record_deliveries(conn, channel, keys):
    """Mark keys as delivered. Call only AFTER a successful send, so a
    failed delivery is retried on the next run (at-least-once, never
    silently dropped)."""
    conn.executemany(
        "INSERT OR IGNORE INTO deliveries (channel, item_key) VALUES (?, ?)",
        [(channel, k) for k in keys],
    )
    conn.commit()
