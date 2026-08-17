"""SQLite persistence.

Deduplication design
--------------------
Each item gets a ``dedup_key`` that is the authoritative "have we seen this?"
identifier for that feed format:

* newsroom   — uses ``raw_id`` (the feed's own ``entry_id``).  The URL carries
               a rotating UTM campaign tag, so the link alone is *not* stable
               across cron runs.
* blogroll   — has no provider-assigned ID at all, so we use ``link``
               (the permalink), which is stable.
* wire/generic — the provider regenerates the GUID whenever an item is edited,
               so we also fall back to ``link``.

``feeds.py`` already picks the right value: it sets ``raw_id`` for newsroom
and ``None`` for the other two.  We derive ``dedup_key`` here as
``raw_id if raw_id else link``.

A ``UNIQUE(source, dedup_key)`` constraint plus ``INSERT OR IGNORE`` means:

* First time an item is seen  → row is inserted, ``rowcount == 1`` → new.
* Every subsequent cron run   → insert is silently ignored, ``rowcount == 0``
  → already seen, do not resend.

``record_new_items`` returns only the items that were actually inserted so
``digest.py`` can send exactly those.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,
    dedup_key   TEXT    NOT NULL,
    raw_id      TEXT,
    title       TEXT    NOT NULL,
    link        TEXT    NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT    NOT NULL DEFAULT (datetime('now')),

    UNIQUE (source, dedup_key)
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_new_items(conn, source, items):
    """Insert items that have not been seen before; return only the new ones.

    Items already in the DB (matched by ``UNIQUE(source, dedup_key)``) are
    silently skipped via ``INSERT OR IGNORE``.  The caller should send only
    the returned subset — that is the entire fix for the duplicate-delivery
    bug.
    """
    new = []
    cur = conn.cursor()
    for item in items:
        dedup_key = item["raw_id"] if item["raw_id"] else item["link"]
        cur.execute(
            "INSERT OR IGNORE INTO items"
            " (source, dedup_key, raw_id, title, link, summary, published)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                source,
                dedup_key,
                item["raw_id"],
                item["title"],
                item["link"],
                item.get("summary"),
                item.get("published"),
            ),
        )
        if cur.lastrowid and cur.rowcount == 1:
            new.append(item)
    conn.commit()
    return new


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
