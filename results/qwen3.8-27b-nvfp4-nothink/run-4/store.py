"""SQLite persistence.

Two tables:

- `items` — archive of everything we have ever seen, mostly so that we can
  answer "did this ever come through?" when someone asks.
- `sent` — the set of items already delivered to *any* channel. This is what
  keeps the 15-minute cron runs from re-sending the same items.

Dedup key
---------

Items are keyed by (source, key), where `key` is the raw provider id when
the feed supplies one, and the item link otherwise. The link is the only
stable identifier across all three feed formats (see feeds.py):

- newsroom: `entry_id` is stable, but the URL rotates tracking params
- generic ("wire"): `guid` is regenerated whenever the item is edited,
  but the link is stable
- blogroll: no id of any kind; only the permalink is stable

Keying on the link (rather than raw_id) for feeds that have ids is
deliberate: if a provider ever changes the id scheme, links are the last
thing to break, and we would rather re-send an edited item than
silently drop it.

A `sent` row is written only after delivery succeeds (see digest.py), so
an item that failed to deliver is retried on the next cron tick.
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_key
    ON items(source, COALESCE(raw_id, link));

CREATE TABLE IF NOT EXISTS sent (
    source      TEXT NOT NULL,
    key         TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, key)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def item_key(item):
    """Per-feed identity of an item. See the module docstring for why."""
    return item.get("raw_id") or item["link"]


def record_items(conn, source, items):
    """Archive newly-seen items; existing keys are left untouched (the
    archive keeps the first-seen copy)."""
    conn.executemany(
        "INSERT OR IGNORE INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in items
        ],
    )
    conn.commit()


def unseen_items(conn, source, items):
    """Of this tick's items, the ones not yet delivered to any channel.

    Called before delivery; the archive is already up to date at this
    point (digest.py records items first).
    """
    sent = load_sent_keys(conn, source)
    return [i for i in items if item_key(i) not in sent]


def load_sent_keys(conn, source):
    """Keys already delivered (to any channel) for one feed."""
    rows = conn.execute(
        "SELECT key FROM sent WHERE source = ?", (source,)
    ).fetchall()
    return {row["key"] for row in rows}


def mark_sent(conn, source, keys):
    """Record that these keys were delivered. Call only after the channel
    accepted the digest."""
    conn.executemany(
        "INSERT OR IGNORE INTO sent (source, key) VALUES (?, ?)",
        [(source, k) for k in keys],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


def count_sent(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM sent").fetchone()["n"]
