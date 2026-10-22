"""SQLite persistence.

The `items` table is the archive of everything we have ever seen, and it is
also what keeps cron repeats from re-delivering: a run only picks items that
are not in the archive yet, so already-sent items are skipped.

Uniqueness of an item is enforced on (source, COALESCE(raw_id, link)) -- the
effective item id -- via a unique index. COALESCE lets feeds that have no
`raw_id` fall back to the item link as the identity.
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

-- Collapse duplicates from before uniqueness was enforced, so the unique
-- index below can be created on databases that already have repeats in them.
DELETE FROM items WHERE id NOT IN (
    SELECT MIN(id) FROM items GROUP BY source, COALESCE(raw_id, link)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_raw
    ON items(source, COALESCE(raw_id, link));
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def unseen_items(conn, source, items):
    """Return the items from `source` that are not in the archive yet."""
    known = {
        row[0]
        for row in conn.execute(
            "SELECT COALESCE(raw_id, link) AS item_id FROM items WHERE source = ?",
            (source,),
        )
    }
    return [item for item in items if (item["raw_id"] or item["link"]) not in known]


def record_items(conn, source, items):
    """Insert `items`, ignoring the ones already in the archive.

    Returns the subset that was genuinely new.
    """
    fresh = []
    for item in items:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO items"
            " (source, raw_id, title, link, summary, published)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                source,
                item["raw_id"],
                item["title"],
                item["link"],
                item.get("summary"),
                item.get("published"),
            ),
        )
        if cursor.rowcount > 0:
            fresh.append(item)
    conn.commit()
    return fresh


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
