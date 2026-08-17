"""SQLite persistence.

Two tables:

* `items` — an append-only log of everything we have seen (one row per
  item per run), mostly so we can answer "did this ever come through?"
  when someone asks. Nothing reads it automatically.
* `sent` — the delivery state that keeps the cron job from re-sending
  items. One row per (channel, source, key): an item may be delivered to
  several channels, but each channel receives it at most once. `key` is
  the stable per-source item identity computed in feeds.py.
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
    source    TEXT NOT NULL,
    key       TEXT NOT NULL,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, key)
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


def sent_keys(conn, channel):
    """The set of (source, key) pairs already delivered to `channel`."""
    return {
        (row["source"], row["key"])
        for row in conn.execute(
            "SELECT source, key FROM sent WHERE channel = ?", (channel,)
        )
    }


def mark_sent(conn, channel, items):
    """Record `items` as delivered to `channel`.

    Call only after the send has succeeded; INSERT OR IGNORE keeps this
    idempotent if a run is ever retried.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, source, key) VALUES (?, ?, ?)",
        [(channel, i["source"], i["key"]) for i in items],
    )
    conn.commit()
