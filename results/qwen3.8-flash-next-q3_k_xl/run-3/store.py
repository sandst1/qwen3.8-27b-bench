"""SQLite persistence.

Two tables:

`items`   an archive of everything we have ever fetched, mostly so that we
          can answer "did this ever come through?" when someone asks. It is
          append-only and may contain the same item many times (once per
          poll that saw it); nothing dedupes it.

`sent`    one row per (channel, item) actually delivered. This is what
          stops cron re-sending the same items on every tick: digest.py
          filters each channel's selection against this table before
          rendering, and records into it only after a successful send, so
          a delivery failure is retried on the next tick rather than
          silently swallowed.

Rows are keyed by `feeds.item_key()` — a per-source identity chosen in
feeds.py, because no single feed field is stable across all providers.
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
    channel   TEXT NOT NULL,
    item_key  TEXT NOT NULL,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
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


def seen_keys(conn, channel):
    """The item keys already delivered to this channel."""
    rows = conn.execute("SELECT item_key FROM sent WHERE channel = ?", (channel,))
    return {r["item_key"] for r in rows}


def mark_sent(conn, channel, keys):
    """Record that these item keys were delivered to this channel.

    Call this only after channels.send() returned successfully; that way a
    failed delivery leaves no trace and the items are retried next tick.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, item_key) VALUES (?, ?)",
        [(channel, k) for k in keys],
    )
    conn.commit()
