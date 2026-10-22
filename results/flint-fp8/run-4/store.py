"""SQLite persistence.

The `items` table is an archive of everything we have ever seen, mostly so
that we can answer "did this ever come through?" when someone asks.

Cron runs us every 15 minutes, so the same feeds get fetched over and over.
Before anything is recorded or sent, `filter_new` is used to tell genuinely new
items apart from ones we have already seen — that is what keeps digests from
re-sending the same items every tick.
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


def _item_key(source, raw_id, link):
    """Stable identity of a feed item within a source.

    Prefers the provider's own id (`raw_id`) when it has one; the blogroll
    format has no id at all, so we fall back to the permalink. The generic
    format's `guid` is deliberately not trusted — see feeds.py.
    """
    return f"{source}:{raw_id or link}"


def seen_keys(conn, source):
    """Set of item keys already in the archive for this source."""
    rows = conn.execute(
        "SELECT raw_id, link FROM items WHERE source = ?", (source,)
    ).fetchall()
    return {_item_key(source, r["raw_id"], r["link"]) for r in rows}


def filter_new(conn, source, items):
    """Return only the items from this fetch that are new to the archive."""
    seen = seen_keys(conn, source)
    fresh = []
    for item in items:
        key = _item_key(source, item.get("raw_id"), item.get("link"))
        if key in seen:
            continue
        seen.add(key)
        fresh.append(item)
    return fresh


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
