"""SQLite persistence.

The `items` table is an archive of everything we have ever seen, mostly so
that we can answer "did this ever come through?" when someone asks. It also
holds the record of what has already gone out, which is how a repeat cron tick
knows an item is not new.
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


def item_key(source, raw_id, link):
    """Identity of an item: its source plus a stable per-item identifier.

    Feeds that have no stable identifier fall back to the item's canonical
    link.
    """
    return (source, raw_id or link)


def seen_keys(conn, source=None):
    """Keys of the items already in the archive, as (source, key) tuples.

    Pass a source to scope the lookup to a single feed; omit it to cover all.
    """
    query = "SELECT source, raw_id, link FROM items"
    params = ()
    if source is not None:
        query += " WHERE source = ?"
        params = (source,)
    keys = set()
    for row in conn.execute(query, params):
        keys.add(item_key(row["source"], row["raw_id"], row["link"]))
    return keys


def filter_new(conn, source, items):
    """The items from `source` that the archive has not seen before."""
    seen = seen_keys(conn, source)
    return [
        i for i in items
        if item_key(source, i.get("raw_id"), i.get("link")) not in seen
    ]


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
