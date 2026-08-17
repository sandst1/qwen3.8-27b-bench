"""SQLite persistence.

Two tables, two different jobs:

* `items` is an archive of every row we've ever fetched, mostly so that we
  can answer "did this ever come through?" when someone asks. It is
  insert-only and expected to contain repeats: the same logical story can
  legitimately show up here multiple times (once per crawl it appeared in),
  keyed loosely by whatever `raw_id` the provider handed us. Nothing reads
  it at the moment.

* `deliveries` is what stops us spamming people. It records, per channel,
  which `dedup_key`s (see feeds.py) have already been sent out. digest.py
  consults this before rendering a channel's digest and filters out
  anything already delivered, then records the new keys after a successful
  send. Keying by channel (not globally) is deliberate: the same item is
  allowed to go to `ops` and `energy` independently if it matches both --
  that's two different audiences seeing it once each, not a duplicate.
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
    channel     TEXT NOT NULL,
    dedup_key   TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, dedup_key)
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


def already_delivered(conn, channel, dedup_keys):
    """Return the subset of `dedup_keys` already sent to `channel`."""
    dedup_keys = list(dedup_keys)
    if not dedup_keys:
        return set()
    placeholders = ",".join("?" for _ in dedup_keys)
    rows = conn.execute(
        f"SELECT dedup_key FROM deliveries WHERE channel = ? AND dedup_key IN ({placeholders})",
        [channel, *dedup_keys],
    )
    return {r["dedup_key"] for r in rows}


def mark_delivered(conn, channel, dedup_keys):
    """Record that `dedup_keys` have now been sent to `channel`."""
    conn.executemany(
        "INSERT OR IGNORE INTO deliveries (channel, dedup_key) VALUES (?, ?)",
        [(channel, k) for k in dedup_keys],
    )
    conn.commit()
