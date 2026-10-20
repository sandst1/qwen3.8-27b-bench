"""SQLite persistence.

Two tables:

- `items` is an append-only archive of everything we have ever seen, so we can
  answer "did this ever come through?" when someone asks. It is not read during
  a normal run; duplicates across runs are expected and harmless.
- `sent` records which items (by feeds.item_key) we have already delivered to
  which channel. digest.py reads it every run so cron does not re-send the same
  story every 15 minutes.
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

-- One row per (channel, item_key) we have successfully delivered.
-- item_key comes from feeds.item_key(); see that function for why it is what
-- it is. The PRIMARY KEY is the dedup guard: a story can never be recorded as
-- sent to a channel twice.
CREATE TABLE IF NOT EXISTS sent (
    channel  TEXT NOT NULL,
    item_key TEXT NOT NULL,
    sent_at  TEXT NOT NULL DEFAULT (datetime('now')),
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


def sent_keys(conn, channel):
    """Item keys already delivered to `channel`, so digest.py can skip them."""
    rows = conn.execute("SELECT item_key FROM sent WHERE channel = ?", (channel,)).fetchall()
    return {r["item_key"] for r in rows}


def mark_sent(conn, channel, keys):
    """Record that `keys` were delivered to `channel`.

    Called only after a channel's send succeeds, so a failed delivery is
    retried on the next cron tick rather than silently dropped.
    """
    if not keys:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, item_key) VALUES (?, ?)",
        [(channel, k) for k in keys],
    )
    conn.commit()
