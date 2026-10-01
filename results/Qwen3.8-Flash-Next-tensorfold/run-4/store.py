"""SQLite persistence.

Two tables:

`items` — an archive of every fetch, so we can answer "did this ever come
through?" when someone asks.

`delivered` — the delivery ledger, one row per (channel, item) that was
successfully sent to a channel. run_once() consults it so that each
repeated item is skipped on later runs and delivery stops being spam.

Identity: an item is identified by (source, canonical link), NOT by its
provider id and NOT by its title/title+link, because the ids we get are not
stable across repeats — the wire provider regenerates its guid whenever an
item is edited, and newsroom appends a rotating utm_campaign to its urls —
while title and link are stable. That is why we normalise the link (drop
query, fragment, trailing slash) before comparing and why title is not part
of the key: an edited or retitled story at the same link is the same story.
If the link is ever empty we fall back to raw_id, then title.

Ledger rows are written only after a successful send, so if delivery to a
channel fails the items are retried on the next tick instead of being
silently consumed.
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
    channel  TEXT NOT NULL,
    source   TEXT NOT NULL,
    link     TEXT NOT NULL,
    sent_at  TEXT NOT NULL DEFAULT (datetime('now')),
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


def item_key(item):
    """The (source, canonical link) pair identifying an item. See module docstring."""
    link = item["link"].split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if not link:
        link = item.get("raw_id") or item["title"]
    return (item["source"], link)


def was_delivered(conn, channel, item):
    source, link = item_key(item)
    row = conn.execute(
        "SELECT 1 FROM delivered WHERE channel = ? AND source = ? AND link = ?",
        (channel, source, link),
    ).fetchone()
    return row is not None


def mark_delivered(conn, channel, items):
    conn.executemany(
        "INSERT OR IGNORE INTO delivered (channel, source, link) VALUES (?, ?, ?)",
        [(channel, *item_key(i)) for i in items],
    )
    conn.commit()
