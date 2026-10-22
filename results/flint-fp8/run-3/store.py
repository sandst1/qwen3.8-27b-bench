"""SQLite persistence.

The `items` table is an archive of everything we have ever sent. It doubles
as the ledger that tells a fresh run which items are new and which are repeats.
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
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    dedupe_key  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if "dedupe_key" not in cols:
        # Databases created before dedupe_key existed get it added on the fly;
        # backfill from the raw identifiers we used before.
        conn.execute("ALTER TABLE items ADD COLUMN dedupe_key TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "UPDATE items SET dedupe_key = COALESCE(raw_id, link) WHERE dedupe_key = ''"
        )
    conn.commit()
    return conn


def is_sent(conn, source, dedupe_key):
    """True if we have already sent an item from this source with this key.

    Items are identified by (source, dedupe_key); that key is the stable
    per-item identity the feed providers expose (entry id, permalink, or —
    for providers that only mint versioned guids — the item URL).
    """
    row = conn.execute(
        "SELECT 1 FROM items WHERE source = ? AND dedupe_key = ? LIMIT 1",
        (source, dedupe_key),
    ).fetchone()
    return row is not None


def mark_sent(conn, source, items):
    """Archive items as sent, skipping anything we have already archived.

    Idempotent per (source, dedupe_key), so calling it twice is harmless. Returns
    the items that were genuinely new.
    """
    known = {
        row[0]
        for row in conn.execute("SELECT dedupe_key FROM items WHERE source = ?", (source,))
    }
    fresh = [i for i in items if i["dedupe_key"] not in known]
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published, dedupe_key)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                source,
                i["raw_id"],
                i["title"],
                i["link"],
                i.get("summary"),
                i.get("published"),
                i["dedupe_key"],
            )
            for i in fresh
        ],
    )
    conn.commit()
    return fresh


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
