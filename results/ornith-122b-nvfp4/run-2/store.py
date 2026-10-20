"""SQLite persistence.

Two tables with two jobs:

* `items` — an archive of everything we have ever fetched, so we can answer
  "did this ever come through?" when someone asks. It is an audit trail; we do
  not read it to decide what to send.
* `deliveries` — the dedup ledger. We record a (channel, key) row for every
  item we actually send, and skip anything a channel has already received.
  See `delivered_keys` and `mark_delivered`.

`deliveries` is created with IF NOT EXISTS, so a database from before this
table existed just gets it on the next run — no migration step.
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

-- One row per (channel, item) we have delivered. `key` is the item's stable
-- anchor (see feeds.py): newsroom keys on its entry_id, while the blogroll and
-- the wire key on their link because their provider ids get edited away.
-- Dedup is per-channel on purpose: a story can belong to several channels in
-- one run, and each of those channels should receive it exactly once.
CREATE TABLE IF NOT EXISTS deliveries (
    channel   TEXT NOT NULL,
    key       TEXT NOT NULL,
    delivered TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, key)
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


def delivered_keys(conn, channel):
    """Keys already delivered to `channel`, across all past runs."""
    rows = conn.execute(
        "SELECT key FROM deliveries WHERE channel = ?", (channel,)
    ).fetchall()
    return {r["key"] for r in rows}


def mark_delivered(conn, channel, items):
    """Record that `items` were delivered to `channel`.

    Items with no key cannot be deduped, so they are not recorded and will be
    offered again next run; every format we ship sets a key.
    """
    rows = [(channel, i["key"]) for i in items if i.get("key")]
    conn.executemany(
        "INSERT OR IGNORE INTO deliveries (channel, key) VALUES (?, ?)", rows
    )
    conn.commit()
