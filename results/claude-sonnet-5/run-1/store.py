"""SQLite persistence.

The `items` table is an archive of everything we have ever seen, mostly so
that we can answer "did this ever come through?" when someone asks.

The `sent` table is the piece that actually prevents duplicate notifications:
it's a ledger of (channel, source, raw_id) that have already been delivered.
digest.py consults it before sending and appends to it after a successful
send, so re-running the same 15-minute cron tick against unchanged feed data
sends nothing.

Dedup key: (channel, source, raw_id). It's per-channel (not just per-item)
because a single item can legitimately go out to more than one channel
(e.g. an "everything" firehose channel alongside a keyword-filtered one) —
those are independent deliveries and each needs its own "have we sent this"
answer. `raw_id` is whatever feeds.py decided is stable for that source's
format (see feeds.py docstring); it is *not* necessarily the provider's own
id field, since two of our three providers hand out ids/urls that change
across re-fetches of the same logical item.
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
    source      TEXT NOT NULL,
    raw_id      TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, raw_id)
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


def already_sent(conn, channel, keys):
    """Given an iterable of (source, raw_id) pairs, return the subset of
    those pairs already delivered to `channel`."""
    keys = list(keys)
    if not keys:
        return set()
    placeholders = ",".join("(?, ?)" for _ in keys)
    params = [v for pair in keys for v in pair]
    rows = conn.execute(
        f"SELECT source, raw_id FROM sent"
        f" WHERE channel = ? AND (source, raw_id) IN ({placeholders})",
        [channel, *params],
    ).fetchall()
    return {(r["source"], r["raw_id"]) for r in rows}


def record_sent(conn, channel, items):
    """Mark `items` (each with 'source' and 'raw_id') as delivered on `channel`."""
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, source, raw_id) VALUES (?, ?, ?)",
        [(channel, i["source"], i["raw_id"]) for i in items],
    )
    conn.commit()
