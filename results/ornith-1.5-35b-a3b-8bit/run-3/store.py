"""SQLite persistence.

The `items` table records every item we have delivered. It has one job: let us
answer "have we already sent this?" before sending again, which is how we stop
the same backlog being pushed on every cron tick.

An item is identified by (source, key), where `key` is the normalized link from
`feeds.identity`. We record an item only once (INSERT OR IGNORE on that key), so
`first_seen` marks when it was first delivered and the table never accumulates
duplicates.

Items are recorded *after* they are sent (see digest.run_once), so a delivery
that fails is retried on the next run rather than silently forgotten.
"""

import sqlite3

import feeds

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    key         TEXT NOT NULL,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_seen ON items(source, key);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def seen_keys(conn):
    """Return the set of (source, key) pairs we have already recorded."""
    return {
        (row["source"], row["key"])
        for row in conn.execute("SELECT source, key FROM items").fetchall()
    }


def record_items(conn, items):
    """Archive the given items, ignoring any already present.

    Keyed on (source, key) via feeds.identity, so re-running against the same
    feed is idempotent and the archive never grows with duplicates.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO items (source, key, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                i["source"],
                feeds.identity(i)[1],
                i["title"],
                i["link"],
                i.get("summary"),
                i.get("published"),
            )
            for i in items
        ],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
