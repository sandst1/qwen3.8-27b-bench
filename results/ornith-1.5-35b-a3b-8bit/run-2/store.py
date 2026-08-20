"""SQLite persistence.

Two tables:

`items` is an append-only archive of everything we have ever seen, mostly so
that we can answer "did this ever come through?" when someone asks. Nothing
else reads it.

`delivered` records what each channel has already been sent, so a re-run never
re-delivers the same item. It is keyed per channel *and* per item identity:

  * per channel  -> a channel that was down for a while gets its backlog when
    it comes back, and one channel never starves another of an item;
  * per identity -> an item is identified by (source, key), where `key` is the
    stable value feeds.py computes for each format.
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

CREATE TABLE IF NOT EXISTS delivered (
    channel   TEXT NOT NULL,
    source    TEXT NOT NULL,
    key       TEXT NOT NULL,
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, key)
);

CREATE INDEX IF NOT EXISTS idx_delivered_channel ON delivered(channel);
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


def delivered_keys(conn, channel):
    """Return the set of (source, key) identities already sent to `channel`."""
    rows = conn.execute(
        "SELECT source, key FROM delivered WHERE channel = ?", (channel,)
    ).fetchall()
    return {(r["source"], r["key"]) for r in rows}


def mark_delivered(conn, channel, identities):
    """Record that `channel` has been sent each (source, key) in `identities`."""
    if not identities:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO delivered (channel, source, key) VALUES (?, ?, ?)",
        [(channel, source, key) for (source, key) in identities],
    )
    conn.commit()
