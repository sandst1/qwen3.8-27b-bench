"""SQLite persistence.

Two tables, one identity rule.

Items are identified by (source, key). `key` is assigned by feeds.py and is
the one field of each feed format that stays stable across fetches and
edits:

    newsroom  -> entry_id   (the URL carries rotating utm_* params)
    blogroll  -> permalink  (the feed has no id of any kind)
    generic   -> link       (the provider regenerates guids on edits)

`items` is the archive of everything we have ever seen, one row per
(source, key), mostly so we can answer "did this ever come through?".

`sent` records which (source, key) each channel has already been notified
about, so a channel only gets items it hasn't seen before. digest.run_once
writes it only after a delivery succeeds, which is what makes a failed
webhook retry on the next cron tick without re-sending to channels that
already got the item.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    key         TEXT,
    raw_id      TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_key ON items(source, key);

CREATE TABLE IF NOT EXISTS sent (
    source      TEXT NOT NULL,
    key         TEXT NOT NULL,
    channel     TEXT NOT NULL,
    first_sent  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, key, channel)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Must run before executescript: the schema indexes `items.key`, which
    # does not exist on DBs created before that column did.
    _migrate(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _migrate(conn):
    # DBs created before `key` existed: add the column and backfill it
    # best-effort. Old generic-feed rows get their (since-regenerated) guid
    # as key, so an item edited since then may gain one extra archive row;
    # harmless, and only happens once per old row.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if cols and "key" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN key TEXT")
        conn.execute("UPDATE items SET key = COALESCE(raw_id, link) WHERE key IS NULL")


def record_items(conn, source, items):
    """Archive items we haven't seen before (one row per (source, key))."""
    existing = {
        row["key"]
        for row in conn.execute("SELECT key FROM items WHERE source = ?", (source,))
    }
    new = [i for i in items if i["key"] not in existing]
    if not new:
        return
    conn.executemany(
        "INSERT INTO items (source, key, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (source, i["key"], i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in new
        ],
    )
    conn.commit()


def sent_keys(conn, channel):
    """The set of (source, key) already delivered to `channel`."""
    return {
        (row["source"], row["key"])
        for row in conn.execute("SELECT source, key FROM sent WHERE channel = ?", (channel,))
    }


def mark_sent(conn, channel, items):
    conn.executemany(
        "INSERT OR IGNORE INTO sent (source, key, channel) VALUES (?, ?, ?)",
        [(i["source"], i["key"], channel) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
