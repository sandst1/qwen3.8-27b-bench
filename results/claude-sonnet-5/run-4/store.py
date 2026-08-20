"""SQLite persistence.

The `items` table is an archive of everything we have ever seen, mostly so
that we can answer "did this ever come through?" when someone asks. Nothing
reads it at the moment.

The `notified` table is what actually prevents re-sending the same item on
every 15-minute cron tick: digest.py checks it before including an item in a
digest, and marks items as notified right after a (non-dry-run) send. It is
keyed on (source, dedupe_key) rather than the row id, because dedupe_key is
the per-format "this is really the same item" identifier computed in
feeds.py (see the module docstring there for why that isn't always raw_id).
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

CREATE TABLE IF NOT EXISTS notified (
    source      TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    notified_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, dedupe_key)
);
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


def unnotified(conn, source, items):
    """Return the subset of items not yet marked notified for this source."""
    if not items:
        return []
    keys = [i["dedupe_key"] for i in items]
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT dedupe_key FROM notified WHERE source = ? AND dedupe_key IN ({placeholders})",
        [source, *keys],
    )
    already = {r["dedupe_key"] for r in rows}
    return [i for i in items if i["dedupe_key"] not in already]


def mark_notified(conn, source, items):
    conn.executemany(
        "INSERT OR IGNORE INTO notified (source, dedupe_key) VALUES (?, ?)",
        [(source, i["dedupe_key"]) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
