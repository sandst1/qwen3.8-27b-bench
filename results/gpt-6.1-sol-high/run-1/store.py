"""SQLite observation archive and per-channel successful-delivery ledger."""

import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
    link_key    TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, link_key)
);
"""


def connect(path):
    conn = sqlite3.connect(path, timeout=60)
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


def item_key(item):
    """Ignore campaign tags and fragments, not article-identifying query args.

    IDs are absent in blogroll and unstable in wire; titles/content are mutable.
    Keep the source so unrelated providers do not suppress each other's items.
    """
    parts = urlsplit(item["link"])
    query = urlencode([
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ])
    link = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))
    return item["source"], link


def pending_items(conn, channel, items):
    """Filter delivered items and duplicates within this poll, preserving order.

    The caller must hold BEGIN IMMEDIATE through selection, send and recording
    so another cron invocation cannot select the same pending delivery.
    """
    selected = []
    seen = set()
    for item in items:
        key = item_key(item)
        if key in seen:
            continue
        seen.add(key)
        delivered = conn.execute(
            "SELECT 1 FROM deliveries WHERE channel = ? AND source = ? AND link_key = ?",
            (channel, *key),
        ).fetchone()
        if delivered is None:
            selected.append(item)
    return selected


def record_delivery(conn, channel, items):
    # Do not commit here: the caller owns the per-channel transaction.
    conn.executemany(
        "INSERT INTO deliveries (channel, source, link_key) VALUES (?, ?, ?)",
        [(channel, *item_key(item)) for item in items],
    )
