"""SQLite persistence.

The `items` table is an observation archive, not proof of delivery. The
separate `deliveries` ledger records successful sends per channel and item.
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
    item_key    TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, item_key)
);
"""


def connect(path):
    conn = sqlite3.connect(path, timeout=30)
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
    """Use the article URL, not editable text or provider-generated GUIDs.

    Remove known tracking parameters and fragments, but retain meaningful
    query parameters (e.g. ?id=123), paths and trailing slashes.
    """
    url = urlsplit(item["link"])
    query = [
        (key, value)
        for key, value in parse_qsl(url.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path,
                       urlencode(query), ""))


def pending_items(conn, channel, items):
    """Select unsent items, also collapsing duplicates within this batch.

    Call inside BEGIN IMMEDIATE and hold that transaction through sending and
    recording delivery, so overlapping processes cannot select the same work.
    """
    pending = []
    seen = set()
    for item in items:
        key = (item["source"], item_key(item))
        if key in seen:
            continue
        seen.add(key)
        delivered = conn.execute(
            "SELECT 1 FROM deliveries WHERE channel = ? AND source = ? AND item_key = ?",
            (channel, *key),
        ).fetchone()
        if delivered is None:
            pending.append(item)
    return pending


def record_delivery(conn, channel, items):
    """Record an acknowledged batch; the caller owns the transaction."""
    conn.executemany(
        "INSERT INTO deliveries (channel, source, item_key) VALUES (?, ?, ?)",
        [(channel, item["source"], item_key(item)) for item in items],
    )
