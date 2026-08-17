"""SQLite persistence.

Two tables:

- `items`: an archive of everything we have ever seen, mostly so that we can
  answer "did this ever come through?" when someone asks. Nothing reads it
  at the moment.
- `sent`: which items have already been delivered to which channel, so the
  15-minute cron run does not re-send them.

Dedup identity is (source, link), NOT the feeds' own ids. The `wire` feed
regenerates guids whenever an item is edited, and `blogroll` has no stable
identifier at all; the permalink is the one field that is stable across
edits for all three feeds. Tracking is per channel (not global) because
channels' keyword filters overlap and an item matching two channels should
appear in both.
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
    channel    TEXT NOT NULL,
    source     TEXT NOT NULL,
    item_link  TEXT NOT NULL,
    first_sent TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, item_link)
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


def unsent_items(conn, channel, items):
    """Return the subset of `items` not yet sent to `channel`."""
    rows = conn.execute(
        "SELECT source, item_link FROM sent WHERE channel = ?", (channel,)
    ).fetchall()
    known = {(r["source"], r["item_link"]) for r in rows}
    return [i for i in items if (i["source"], i["link"]) not in known]


def mark_sent(conn, channel, items):
    """Record `items` as delivered to `channel`.

    Called only after a successful send: if the channel is down the
    exception propagates and the next cron tick retries them.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, source, item_link)"
        " VALUES (?, ?, ?)",
        [(channel, i["source"], i["link"]) for i in items],
    )
    conn.commit()


def prune_sent(conn, older_than_days=30):
    """Drop old sent-records so the table cannot grow without bound.

    Trade-off: an item that stays in a feed longer than the retention
    window is sent again. That is acceptable — feeds only keep a recent
    window of items, and an occasional re-send is a mild annoyance, not a
    failure.
    """
    conn.execute(
        "DELETE FROM sent WHERE first_sent < datetime('now', ?)",
        (f"-{older_than_days} days",),
    )
    conn.commit()
