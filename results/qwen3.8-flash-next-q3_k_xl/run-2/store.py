"""SQLite persistence.

The `items` table is an archive of everything we have ever seen, mostly so
that we can answer "did this ever come through?" when someone asks.

The `sent` table is the dedup ledger: one row per (channel, item) actually
delivered. digest.py consults it before each send so cron re-runs only
deliver genuinely new items. Item identity is feeds.item_key() — see the
docstring there for why it is the normalised link and not a provider id.
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

CREATE TABLE IF NOT EXISTS sent (
    channel     TEXT NOT NULL,
    item_key    TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, item_key)
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


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


def already_sent(conn, channel):
    """Set of item keys this channel has already received."""
    rows = conn.execute("SELECT item_key FROM sent WHERE channel = ?", (channel,))
    return {r["item_key"] for r in rows}


def mark_sent(conn, channel, keys):
    """Record delivery only after channels.send() returned successfully,
    so a failed delivery is retried on the next cron tick."""
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, item_key) VALUES (?, ?)",
        [(channel, k) for k in keys],
    )
    conn.commit()
