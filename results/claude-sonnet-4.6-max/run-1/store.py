"""SQLite persistence.

The `items` table records every item we have dispatched.  It serves two
purposes:

1. Deduplication — ``seen_links`` is called on every run so that
   ``run_once`` can skip items that have already been sent.

2. Audit log — you can query the DB to answer "did this ever come through?"

Deduplication key
-----------------
We use ``link`` (the canonical URL) rather than ``raw_id`` because:

* The *blogroll* feed has no stable identifier at all (``raw_id`` is always
  ``None``).
* The *wire* (generic) feed regenerates its guid whenever an item is edited
  (see comment in feeds.py), so the same article can appear with a different
  guid on successive fetches.  Its URL, however, does not change.
* The *newsroom* feed has a stable ``entry_id``, but ``link`` works just as
  well and keeps the dedup logic uniform across all three sources.
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


def seen_links(conn, source):
    """Return the set of links already recorded for *source*.

    Used by ``run_once`` to discard items that were dispatched on a previous
    run before they reach the send loop.
    """
    rows = conn.execute(
        "SELECT link FROM items WHERE source = ?", (source,)
    ).fetchall()
    return {row["link"] for row in rows}


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
