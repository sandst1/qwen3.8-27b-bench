"""SQLite observation archive and independent per-channel delivery ledger.

An observation is not proof of delivery. Existing archive rows therefore never
seed the ledger. See README.md for identity and retry semantics.
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
    # A competing cron invocation may be waiting on a webhook send (10s timeout).
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
    """Use permalinks, not mutable content or missing/regenerated provider IDs.

    Strip only known tracking parameters and fragments; meaningful query values
    and path case must continue to distinguish different articles.
    """
    url = urlsplit(item["link"])
    query = sorted(
        (key, value)
        for key, value in parse_qsl(url.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"fbclid", "gclid"}
    )
    return urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path,
                       urlencode(query), ""))


def pending_items(conn, channel, items):
    """Filter prior deliveries and duplicates within this fetched batch."""
    selected = []
    seen = set()
    for item in items:
        identity = (item["source"], item_key(item))
        if identity in seen:
            continue
        seen.add(identity)
        delivered = conn.execute(
            "SELECT 1 FROM deliveries WHERE channel = ? AND source = ? AND item_key = ?",
            (channel, *identity),
        ).fetchone()
        if delivered is None:
            selected.append(item)
    return selected


def record_delivery(conn, channel, items):
    """Caller commits after sending, while still holding the delivery lock."""
    conn.executemany(
        "INSERT INTO deliveries (channel, source, item_key) VALUES (?, ?, ?)",
        [(channel, item["source"], item_key(item)) for item in items],
    )
