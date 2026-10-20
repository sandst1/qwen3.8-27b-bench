"""SQLite persistence.

The `items` table is an observation archive, not proof of delivery.
`deliveries` records successful sends per channel and stable source identity.
Keep the two separate: fetching or previewing an item must not suppress it.
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
    channel     TEXT NOT NULL,
    source      TEXT NOT NULL,
    identity    TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, identity)
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


def pending_items(conn, channel, items):
    """Call under BEGIN IMMEDIATE, held through sending and recording receipts."""
    return [
        item for item in items
        if conn.execute(
            "SELECT 1 FROM deliveries WHERE channel = ? AND source = ? AND identity = ?",
            (channel, item["source"], item["identity"]),
        ).fetchone() is None
    ]


def record_deliveries(conn, channel, items):
    # The caller commits after send succeeds. Never commit inside this helper.
    conn.executemany(
        "INSERT INTO deliveries (channel, source, identity) VALUES (?, ?, ?)",
        [(channel, item["source"], item["identity"]) for item in items],
    )
