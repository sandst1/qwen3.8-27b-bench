"""SQLite persistence.

The `items` table doubles as the seen-items ledger: an archive of everything
we have ever seen, and the record the digest loop consults to tell new items
from repeats. Only items that are not in the archive yet get dispatched.
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
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def item_key(source, item):
    """Identity of an item within a source: the feed's raw id when it has
    one, else the permalink. Blogroll posts have no raw id, so they key on
    their link instead."""
    return source, item.get("raw_id") or item["link"]


def unseen_items(conn, source, items):
    """The items from this source that are not in the archive yet."""
    fresh = []
    for item in items:
        key_source, key_value = item_key(source, item)
        row = conn.execute(
            "SELECT 1 FROM items WHERE source = ? AND COALESCE(raw_id, link) = ?",
            (key_source, key_value),
        ).fetchone()
        if row is None:
            fresh.append(item)
    return fresh


def record_items(conn, source, items):
    """Archive the given items. Callers pass only the items reported as new
    by `unseen_items`, so every real item ends up with exactly one row."""
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
