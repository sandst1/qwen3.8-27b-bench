"""SQLite persistence.

The `items` table is an archive of everything we have ever seen, mostly so
that we can answer "did this ever come through?" when someone asks.

The `sent` table tracks which items have been delivered to which channel so
we never send the same item twice.  We use the item *link* as the dedup key
because it is the most stable identifier across all feed formats:

  - newsroom: has a stable entry_id, but the URL is equally stable.
  - blogroll: has no machine identifier at all; permalink is all we have.
  - wire/generic: the guid is regenerated on every edit (typo fixes, retitles)
    so it is useless for dedup; the link stays constant.

If a feed ever serves two semantically-different items at the same URL we
will miss the second one.  In practice these feeds don't do that — a URL
identifies a single story.
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_link ON items(source, link);

CREATE TABLE IF NOT EXISTS sent (
    channel     TEXT NOT NULL,
    link        TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, link)
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
        "INSERT OR IGNORE INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in items
        ],
    )
    conn.commit()


def unsent_links(conn, channel, links):
    """Return the subset of *links* not yet delivered to *channel*."""
    if not links:
        return set()
    placeholders = ",".join("?" * len(links))
    rows = conn.execute(
        f"SELECT link FROM sent WHERE channel = ? AND link IN ({placeholders})",
        [channel] + list(links),
    ).fetchall()
    already = {r["link"] for r in rows}
    return set(links) - already


def mark_sent(conn, channel, links):
    """Record that *links* were delivered to *channel*."""
    if not links:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, link) VALUES (?, ?)",
        [(channel, link) for link in links],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
