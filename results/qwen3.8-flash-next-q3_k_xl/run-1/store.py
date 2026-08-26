"""SQLite persistence.

Two tables with two different jobs:

`items` is an archive of everything we have ever seen, mostly so that we can
answer "did this ever come through?" when someone asks. It is append-only and
is not consulted when deciding what to send.

`deliveries` is the memory that stops people receiving the same item twice.
It records one row per (channel, item key) that we have successfully handed to
a channel. digest.py reads it before sending and writes to it only after a
send succeeds, so a channel that is down simply retries on the next tick
rather than silently dropping the item. The key is the stable identity
computed in feeds.py -- see the notes there for why it is not just the raw_id
or the link.
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

CREATE TABLE IF NOT EXISTS deliveries (
    channel       TEXT NOT NULL,
    item_key      TEXT NOT NULL,
    delivered_at  TEXT NOT NULL DEFAULT (datetime('now')),
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


def already_delivered(conn, channel):
    """The set of item keys already delivered to `channel`.

    Used to filter a freshly fetched batch down to items this channel has not
    seen yet.
    """
    rows = conn.execute(
        "SELECT item_key FROM deliveries WHERE channel = ?", (channel,)
    ).fetchall()
    return {row["item_key"] for row in rows}


def mark_delivered(conn, channel, keys):
    """Record that `channel` received the items with these keys.

    Call only after the channel has accepted the digest; INSERT OR IGNORE
    keeps this safe to re-run.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO deliveries (channel, item_key) VALUES (?, ?)",
        [(channel, key) for key in keys],
    )
    conn.commit()
