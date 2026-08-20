"""SQLite persistence.

Two tables:

- `items`  append-only archive of everything we have ever seen, mostly so we
  can answer "did this ever come through?" when someone asks.
- `sent`   what each channel has already been delivered. This is the whole
  reason the database exists: it is how we stop re-sending the same items on
  every cron tick. See `newly_sent` / `mark_sent`.
"""

import sqlite3
import urllib.parse

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

-- One row per (channel, item) we have delivered. PRIMARY KEY makes INSERT
-- OR IGNORE idempotent, so a retry never double-counts.
CREATE TABLE IF NOT EXISTS sent (
    channel   TEXT NOT NULL,
    source    TEXT NOT NULL,
    link      TEXT NOT NULL,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, link)
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


def canonical_link(link):
    """Stable identity for a feed item.

    The feeds disagree on identifiers: newsroom ids are stable, but the generic
    feed regenerates its guid on every edit and the blogroll has no id at all,
    so `raw_id` cannot be trusted. The one thing that survives an edit is the
    link, so we key on it. The query string is dropped because providers rewrite
    tracking params (utm_*, ...) on every pull, which would otherwise make a
    stable item look new.
    """
    if not link:
        return ""
    parts = urllib.parse.urlsplit(link)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def newly_sent(conn, channel, rows):
    """Return the subset of `rows` this `channel` has not been sent yet.

    `rows` are (source, link) pairs built with canonical_link() for the items
    that matched this channel this run. Only items a channel has never seen are
    returned, so re-runs deliver nothing twice.
    """
    if not rows:
        return []
    already = {
        (r["source"], r["link"])
        for r in conn.execute(
            "SELECT source, link FROM sent WHERE channel = ?", (channel,)
        )
    }
    return [row for row in rows if row not in already]


def mark_sent(conn, channel, rows):
    """Record that `rows` (source, link) have been delivered to `channel`.

    Call this only after delivery succeeds, so a failed send is retried next
    tick rather than silently skipped.
    """
    if not rows:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, source, link) VALUES (?, ?, ?)",
        [(channel, source, link) for source, link in rows],
    )
    conn.commit()
