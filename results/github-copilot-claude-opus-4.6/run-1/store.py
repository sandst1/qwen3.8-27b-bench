"""SQLite persistence and deduplication.

Design decision (2026-08-16): We deduplicate using a `sent_items` table keyed
on (channel, item_fingerprint).  The fingerprint is the item's `raw_id` if
available, otherwise its `link`.  This means an item is sent to a channel at
most once, even if cron fires repeatedly.  The old `items` table is kept as an
append-only archive for auditability.
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
    channel     TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, fingerprint)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _fingerprint(item):
    """Stable identifier for an item: prefer raw_id, fall back to link."""
    return item.get("raw_id") or item["link"]


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


def filter_unsent(conn, channel, items):
    """Return only items not yet sent to *channel*."""
    unsent = []
    for item in items:
        fp = _fingerprint(item)
        row = conn.execute(
            "SELECT 1 FROM sent_items WHERE channel = ? AND fingerprint = ?",
            (channel, fp),
        ).fetchone()
        if row is None:
            unsent.append(item)
    return unsent


def mark_sent(conn, channel, items):
    """Record that *items* have been sent to *channel*."""
    conn.executemany(
        "INSERT OR IGNORE INTO sent_items (channel, fingerprint) VALUES (?, ?)",
        [(channel, _fingerprint(i)) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
