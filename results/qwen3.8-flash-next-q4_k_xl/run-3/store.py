"""SQLite persistence.

Two tables:

`items` is an archive of everything we have ever seen, keyed by a unique
`uid` (see feeds.py for how identity is decided). Inserting an item we
already archived is a no-op.

`deliveries` is the dedup ledger: one row per (channel, uid), written only
after a channel's digest has been sent successfully. This is what stops
people getting the same items every 15 minutes. It is per channel, so an
item matching two channels is still delivered to both, and because rows
are written after a successful send, a channel whose webhook was down
retries on the next cron tick.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL,
    source      TEXT NOT NULL,
    raw_id      TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_uid ON items(uid);

CREATE TABLE IF NOT EXISTS deliveries (
    channel     TEXT NOT NULL,
    uid         TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, uid)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _migrate(conn):
    """Upgrade databases created before items had a uid column.

    Old archives contain the same item many times (that was the bug), so
    we backfill a uid, drop the duplicate rows, and only then let SCHEMA
    add the unique index. We backfill from the link rather than raw_id:
    for old wire rows the guid may already have changed, but the worst
    case is that a handful of items get sent one last time.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if not cols or "uid" in cols:
        return
    conn.execute("ALTER TABLE items ADD COLUMN uid TEXT")
    conn.execute("UPDATE items SET uid = source || '|' || link")
    conn.execute(
        "DELETE FROM items WHERE id NOT IN"
        " (SELECT MIN(id) FROM items GROUP BY uid)"
    )
    conn.commit()


def record_items(conn, items):
    conn.executemany(
        "INSERT OR IGNORE INTO items (uid, source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                i["uid"],
                i["source"],
                i["raw_id"],
                i["title"],
                i["link"],
                i.get("summary"),
                i.get("published"),
            )
            for i in items
        ],
    )
    conn.commit()


def delivered_uids(conn, channel):
    """Uids this channel has already received."""
    rows = conn.execute("SELECT uid FROM deliveries WHERE channel = ?", (channel,))
    return {row["uid"] for row in rows}


def mark_delivered(conn, channel, uids):
    conn.executemany(
        "INSERT OR IGNORE INTO deliveries (channel, uid) VALUES (?, ?)",
        [(channel, uid) for uid in uids],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
