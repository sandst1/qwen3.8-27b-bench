"""SQLite persistence.

Two tables:

* ``items`` — an archive of every distinct item we have ever seen, mostly
  so that we can answer "did this ever come through?" when someone asks.

* ``sent`` — which items have already been delivered to which channel.
  This is what stops the 15-minute cron run from resending the same
  digest.

Item identity
-------------
The three feeds disagree on stable identifiers, so an item is considered
the same across pollings if it has the same (source, raw_id) OR the same
(source, link):

* newsroom keeps its entry_id but rewrites the URL (rotating utm params);
* the wire feed keeps its link but regenerates the guid on every edit;
* blogroll has no id at all, only a permalink.

Matching on raw_id OR link means an edited wire item (new guid, same
link) is not re-archived or resent, and a newsroom item whose URL gained
utm params (same entry_id) is not either.

``sent`` is keyed per channel on purpose: if a channel's keyword list
changes or a new channel is added, its next run should still get the
items that match it; and a failed delivery must not count as sent, so
the next tick retries it.

Existing databases are upgraded in place: ``connect()`` creates the
``sent`` table on the next run. Pre-existing duplicate rows in ``items``
are left alone.
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
    channel   TEXT NOT NULL,
    source    TEXT NOT NULL,
    raw_id    TEXT,
    link      TEXT NOT NULL,
    title     TEXT,
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


def item_seen(known_ids, known_links, item):
    """True if `item` matches something already recorded.

    Matches on raw_id OR link; see the module docstring for why both.
    """
    raw_id = item.get("raw_id")
    if raw_id and raw_id in known_ids:
        return True
    return item["link"] in known_links


def archived_pairs(conn, source):
    """Return (raw_ids, links) already in the `items` archive for `source`."""
    rows = conn.execute(
        "SELECT raw_id, link FROM items WHERE source = ?", (source,)
    ).fetchall()
    return ({r["raw_id"] for r in rows if r["raw_id"]}, {r["link"] for r in rows})


def sent_pairs(conn, channel):
    """Return (raw_ids, links) already delivered to `channel`."""
    rows = conn.execute(
        "SELECT raw_id, link FROM sent WHERE channel = ?", (channel,)
    ).fetchall()
    return ({r["raw_id"] for r in rows if r["raw_id"]}, {r["link"] for r in rows})


def record_items(conn, source, items):
    """Archive items we have not seen before (see `item_seen`)."""
    known_ids, known_links = archived_pairs(conn, source)
    new = [i for i in items if not item_seen(known_ids, known_links, i)]
    if not new:
        return
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in new
        ],
    )
    conn.commit()


def mark_sent(conn, channel, items):
    """Record that `items` were just delivered to `channel`.

    Call this only after a successful send, so a failed delivery is
    retried on the next tick.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, source, raw_id, link, title)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (channel, i["source"], i["raw_id"], i["link"], i["title"])
            for i in items
        ],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
