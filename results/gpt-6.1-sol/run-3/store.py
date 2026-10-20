"""SQLite observation archive and per-channel successful-delivery ledger.

An observation is not proof of delivery. Keep the append-only archive separate
from the ledger, including when opening databases created before the ledger.
"""

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
    link        TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, link)
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
    """Use the source and permalink, ignoring UTM tracking and fragments.

    Raw IDs can be absent or regenerated; titles and summaries can be edited.
    Keep other query parameters: they may distinguish different articles.
    Different sources remain distinct even if they link to the same article.
    """
    parts = urlsplit(item["link"])
    query = urlencode([
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ])
    link = urlunsplit(parts._replace(query=query, fragment=""))
    return item["source"], link


def undelivered(conn, channel, items):
    """Filter both previous deliveries and duplicates within this batch.

    Caller holds BEGIN IMMEDIATE through sending and recording, so another
    cron process cannot select the same unsent batch concurrently.
    """
    selected = []
    seen = set()
    for item in items:
        key = item_key(item)
        if key in seen:
            continue
        seen.add(key)
        delivered = conn.execute(
            "SELECT 1 FROM deliveries WHERE channel = ? AND source = ? AND link = ?",
            (channel, *key),
        ).fetchone()
        if delivered is None:
            selected.append(item)
    return selected


def record_deliveries(conn, channel, items):
    """Record successful sends; caller commits each channel independently."""
    conn.executemany(
        "INSERT INTO deliveries (channel, source, link) VALUES (?, ?, ?)",
        [(channel, *item_key(item)) for item in items],
    )
