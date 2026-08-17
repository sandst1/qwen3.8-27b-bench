"""SQLite persistence.

The `items` table serves two purposes:
  1. Deduplication: seen_links() is called at the start of every run so that
     only genuinely new items are sent.  This is what prevents people from
     receiving the same digest items over and over.
  2. Archive: a full history of every item ever seen, useful for debugging.

Deduplication key
-----------------
We use `link` (the canonical URL, UTM-stripped by feeds.py) as the identity
for each item.  Reasons:

* newsroom raw_ids are stable, but the URLs they publish have rotating UTM
  campaign tags — feeds.py strips those so the canonical URL is stable.
* blogroll items have no raw_id at all; permalink is the only identifier.
* wire guids are regenerated on every edit; the link stays constant.

The UNIQUE index on `link` enforces this at the database level.  On an
existing database that was populated before deduplication was added (and
therefore may contain duplicate links), CREATE UNIQUE INDEX will fail;
connect() handles that gracefully — seen_links() still works correctly
because it returns a Python set of DISTINCT links.
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
    # Add the unique index separately so that an existing database that already
    # contains duplicate links (from before this fix) doesn't crash on startup.
    # The Python-level seen_links() filter is the primary dedup guard; the
    # unique index is belt-and-suspenders for fresh databases.
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_items_link ON items(link)"
        )
    except sqlite3.OperationalError:
        pass  # Pre-existing duplicates prevent the index; seen_links() still works.
    conn.commit()
    return conn


def seen_links(conn):
    """Return the set of canonical links already stored in the archive.

    Called once per run before any items are processed.  Items whose link is
    in this set have already been sent and must be skipped.
    """
    rows = conn.execute("SELECT DISTINCT link FROM items").fetchall()
    return {row["link"] for row in rows}


def record_items(conn, source, items):
    """Persist *items* to the archive.

    Uses INSERT OR IGNORE so that if the unique index exists a race between
    two concurrent cron ticks cannot insert the same link twice.  When the
    caller has already filtered with seen_links() this is a no-op safety net.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO items"
        " (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in items
        ],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
