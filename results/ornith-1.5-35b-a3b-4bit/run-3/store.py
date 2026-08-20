"""SQLite persistence.

The `items` table is an archive of everything we have ever seen. It doubles as
the dedup log: `digest` delivers each item at most once, keyed on the item's
link, by checking what is already in this table.
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


def normalize_link(link):
    """Stable identity key for an item, derived from its link.

    The link is the one field every feed format shares and the least likely to
    change. raw_id is unusable as a key: blogroll has none, and the generic
    provider regenerates its guid whenever an item is edited. Stripping the
    query string keeps tracking params (utm_campaign, and friends) from making
    the same item look new.
    """
    return link.split("?", 1)[0].split("#", 1)[0]


def seen_links(conn):
    """Return the normalized links already archived from previous runs."""
    return {
        normalize_link(row["link"])
        for row in conn.execute("SELECT link FROM items").fetchall()
    }
