"""SQLite persistence.

The `items` table is both the delivery ledger and the archive: a row is only
written once an item's digest has actually been sent, and the next run reads
it back (see `get_seen`) to decide which items are new. The archive use — "did
this ever come through?" — keeps the original purpose alive.
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


def item_key(source, raw_id, link):
    """Stable identity for an item: (source, canonical link).

    The query string is stripped from the link so that tracking params (e.g.
    utm_*) or feed-specific suffixes don't make the same item look new on every
    fetch. Falls back to the raw id when there is no link.
    """
    identity = (link or "").split("?", 1)[0] or (raw_id or "")
    return (source, identity)


def get_seen(conn, source):
    """Return the set of item keys for `source` already in the archive."""
    rows = conn.execute(
        "SELECT source, raw_id, link FROM items WHERE source = ?", (source,)
    ).fetchall()
    return {item_key(row["source"], row["raw_id"], row["link"]) for row in rows}
