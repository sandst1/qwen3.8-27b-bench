"""SQLite persistence.

The `items` table is an archive of everything we have ever seen. It doubles
as the dedup state for the digest: `run_once` only sends items that have not
been recorded before, so an item is delivered at most once.

Identity is the normalised link, not `raw_id`:

- the blogroll feed has no identifier at all;
- the wire feed regenerates its guid whenever an item is edited;
- newsroom URLs differ between polls (rotating utm_campaign parameter).

Links are normalised in feeds.py (utm_* params stripped) before they get
here, so a feed that decorates its URLs with tracking params cannot
defeat dedup. `raw_id` is kept for provenance only and is NOT part of
identity.

The UNIQUE index on link is what makes "already seen" cheap and also keeps
the archive from accumulating one row per poll of the same item.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    raw_id      TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL UNIQUE,
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
    """Insert items not yet seen; return the ones that are new.

    "New" means the link has never been recorded, regardless of source or
    raw_id. If a feed ever relinks an item (new URL, same story) it will be
    delivered again; that is the right trade-off — a wrong link is easier
    to live with than a silently dropped item.
    """
    new_items = []
    for item in items:
        row = conn.execute(
            "SELECT 1 FROM items WHERE link = ?", (item["link"],)
        ).fetchone()
        if row is not None:
            continue
        conn.execute(
            "INSERT INTO items (source, raw_id, title, link, summary, published)"
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
        new_items.append(item)
    conn.commit()
    return new_items


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
