"""SQLite persistence.

Two tables:

- `items`: an archive of everything we have ever seen, mostly so that we can
  answer "did this ever come through?" when someone asks.
- `sent`: the dedup ledger. After an item is delivered to a channel it is
  recorded here, so the next cron run does not send it again. This is what
  stops people from receiving the same items over and over.

Identity is (source, key): `key` is the per-item stable identifier chosen in
feeds.py, where each feed format knows which of its fields is actually
stable.

Markers expire after SENT_RETENTION_DAYS so the table cannot grow without
bound; an item that reappears after that is treated as new and sent again.
"""

import sqlite3

# How long to remember that we sent an item. Long enough that feeds which
# keep re-serving recent items never cause a repeat; short enough that the
# table stays small. If a feed re-serves something older than this, it is
# considered new and gets sent again.
SENT_RETENTION_DAYS = 30

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
    source   TEXT NOT NULL,
    key      TEXT NOT NULL,
    channel  TEXT NOT NULL,
    sent_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, key, channel)
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
    """Set of (source, key) pairs already delivered to `channel`."""
    return {
        (row["source"], row["key"])
        for row in conn.execute(
            "SELECT source, key FROM sent WHERE channel = ?", (channel,)
        )
    }


def mark_sent(conn, channel, items):
    """Record that `items` were delivered to `channel`. Call after the send
    succeeds, not before: then a failed channel is retried on the next
    tick while channels that already got the items are not re-sent."""
    conn.executemany(
        "INSERT OR IGNORE INTO sent (source, key, channel) VALUES (?, ?, ?)",
        [(i["source"], i["key"], channel) for i in items],
    )
    conn.commit()


def prune_sent(conn):
    """Drop markers older than SENT_RETENTION_DAYS."""
    conn.execute(
        "DELETE FROM sent WHERE sent_at < datetime('now', ?)",
        (f"-{SENT_RETENTION_DAYS} days",),
    )
    conn.commit()
