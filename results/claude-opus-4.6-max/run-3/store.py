"""SQLite persistence.

The ``items`` table is an archive of everything we have ever fetched.
The ``sent`` table tracks which items have already been delivered to each
channel so that ``digest.py`` never sends the same item twice.

De-duplication strategy
-----------------------
Each feed item's *link* is the most stable identifier across all three
feed formats.  ``raw_id`` is unreliable: the blogroll provider omits it
entirely, and the wire provider regenerates the GUID on every edit.
The newsroom provider's ``entry_id`` is stable but its feed URLs carry
UTM campaign parameters that rotate weekly.

We therefore derive a *link_key* — the link stripped of query string and
fragment — and use it as the identity of an item in the ``sent`` table.
This correctly collapses:

* the same newsroom article across UTM rotations,
* wire edits that only change the GUID, and
* blogroll updates that only change the title or excerpt.

The ``items`` archive uses ``(source, link)`` as a unique key.  This
prevents the exact-same fetch from producing duplicate rows (the common
case when cron fires every 15 minutes and the feed hasn't changed), while
still archiving a new row when the link genuinely changes (e.g. a weekly
UTM rotation).
"""

import sqlite3
from urllib.parse import urlparse, urlunparse

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

CREATE UNIQUE INDEX IF NOT EXISTS uq_items_source_link ON items(source, link);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

CREATE TABLE IF NOT EXISTS sent (
    channel     TEXT NOT NULL,
    link_key    TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, link_key)
);
"""


def _normalize_link(link):
    """Strip query string and fragment to produce a stable identity key.

    Feed URLs often carry tracking parameters (UTM tags, session tokens)
    that change between fetches.  The path alone is stable across all
    three providers we pull from.
    """
    p = urlparse(link)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_items(conn, source, items):
    conn.executemany(
        "INSERT OR IGNORE INTO items"
        " (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"],
             i.get("summary"), i.get("published"))
            for i in items
        ],
    )
    conn.commit()


def unsent_for_channel(conn, channel, items):
    """Return the subset of *items* not yet delivered to *channel*."""
    already = {
        row["link_key"]
        for row in conn.execute(
            "SELECT link_key FROM sent WHERE channel = ?", (channel,)
        ).fetchall()
    }
    return [i for i in items if _normalize_link(i["link"]) not in already]


def mark_sent(conn, channel, items):
    """Record that *items* have been delivered to *channel*."""
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, link_key) VALUES (?, ?)",
        [(channel, _normalize_link(i["link"])) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
