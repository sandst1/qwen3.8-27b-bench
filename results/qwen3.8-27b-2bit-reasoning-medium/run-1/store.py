"""SQLite persistence.

Two tables:

* `items` -- an observation log: one row per item per fetch. Kept so we can
  answer "did this ever come through?" when someone asks. Deliberately not
  deduplicated; the dedup record is `sent`.
* `sent`  -- one row per (source, link, channel) for the moment an item was
  successfully delivered to that channel. digest.py only sends items that
  are not here yet for that channel, which is what stops the 15-minute cron
  from re-sending the same items every tick.

Why does dedup key on (source, link) instead of raw_id? See `item_key`.
"""

import sqlite3
from urllib.parse import urlsplit, urlunsplit

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
    source     TEXT NOT NULL,
    link       TEXT NOT NULL,
    channel    TEXT NOT NULL,
    first_sent TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, link, channel)
);
"""


def item_key(item):
    """The (source, link) pair that identifies an item across runs.

    Deliberately NOT raw_id: blogroll has no identifier at all, and the wire
    feed regenerates its guid whenever an item is edited (typos, retitles),
    which would re-send edited items. Deliberately NOT the raw link either:
    newsroom appends rotating utm_* tracking params to the same article, so
    the raw link changes week to week. Stripping the query string and
    fragment leaves a permalink that is stable for all three providers.
    """
    parts = urlsplit(item["link"])
    return item["source"], urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


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


def sent_keys(conn, channel):
    """Set of item keys already delivered to `channel`."""
    return {
        (row["source"], row["link"])
        for row in conn.execute("SELECT source, link FROM sent WHERE channel = ?", (channel,))
    }


def mark_sent(conn, channel, items):
    """Record that `items` were delivered to `channel`.

    Call this only after a successful send: if delivery fails, the items
    stay unmarked and get retried on the next cron tick.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO sent (source, link, channel) VALUES (?, ?, ?)",
        [(*item_key(i), channel) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
