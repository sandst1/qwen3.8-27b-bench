"""SQLite persistence.

The `items` table archives observations, not delivery receipts. The separate
`deliveries` ledger records successful sends per channel and article identity.
"""

import sqlite3
from contextlib import contextmanager
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
    article_url TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, article_url)
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
    """Ignore tracking and edits, but preserve source and meaningful URL parts.

    All current providers have article permalinks; their IDs are absent or
    unstable. Keep this policy stable: changing it can resend existing items.
    """
    url = urlsplit(item["link"])
    query = sorted(
        (key, value)
        for key, value in parse_qsl(url.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"fbclid", "gclid"}
    )
    canonical = urlunsplit(
        (url.scheme.lower(), url.netloc.lower(), url.path, urlencode(query), "")
    )
    return item["source"], canonical


@contextmanager
def delivery_transaction(conn):
    """Serialize selection AND sending across cron processes using this DB.

    Acquire the write lock before reading receipts, not just when inserting
    them. Otherwise two processes can both decide the same item is unsent.
    Commit each channel independently so a later failure cannot undo success.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def undelivered(conn, channel, items):
    selected = []
    seen = set()
    for item in items:
        key = item_key(item)
        if key in seen:
            continue
        seen.add(key)
        receipt = conn.execute(
            "SELECT 1 FROM deliveries WHERE channel = ? AND source = ? AND article_url = ?",
            (channel, *key),
        ).fetchone()
        if receipt is None:
            selected.append(item)
    return selected


def record_deliveries(conn, channel, items):
    """Called only after send succeeds, inside delivery_transaction."""
    conn.executemany(
        "INSERT INTO deliveries (channel, source, article_url) VALUES (?, ?, ?)",
        [(channel, *item_key(item)) for item in items],
    )
