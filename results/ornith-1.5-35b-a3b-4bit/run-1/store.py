"""SQLite persistence.

Two tables serve two different jobs:

`items` is an append-only archive of everything we have ever seen. It answers
"did this ever come through, and what did it say?" when someone asks. Rows are
inserted idempotently (on source + canonical key) so re-pulling the same feed
does not pile up duplicate rows.

`delivered` records, per channel, which item keys we have already sent. It is
what keeps the same item from being pushed twice. Deduplication is per channel
on purpose: an item can legitimately belong to more than one channel (the
"everything" firehose matches every keyword), so a single global "seen" set
would hand each item to only the first matching channel and starve the rest.

Identity is the item's canonical `key` (see feeds.canonical_key): the URL with
the query string and fragment stripped. We key on that rather than `raw_id`
(blogroll has none; the generic wire feed regenerates its guid on every edit)
and rather than the raw link (providers append per-crawl tracking params such
as `?utm_campaign=...` that change while the article does not).
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    raw_id      TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    key         TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, key)
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

CREATE TABLE IF NOT EXISTS delivered (
    channel      TEXT NOT NULL,
    key          TEXT NOT NULL,
    delivered_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, key)
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
        "INSERT OR IGNORE INTO items (source, raw_id, title, link, key, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                source,
                i["raw_id"],
                i["title"],
                i["link"],
                i["key"],
                i.get("summary"),
                i.get("published"),
            )
            for i in items
        ],
    )
    conn.commit()


def delivered_keys(conn, channel):
    """Item keys already sent to `channel`, as a set."""
    return {
        r["key"]
        for r in conn.execute(
            "SELECT key FROM delivered WHERE channel = ?", (channel,)
        )
    }


def mark_delivered(conn, channel, keys):
    """Record that `keys` were sent to `channel`. Idempotent."""
    if not keys:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO delivered (channel, key) VALUES (?, ?)",
        [(channel, key) for key in keys],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
