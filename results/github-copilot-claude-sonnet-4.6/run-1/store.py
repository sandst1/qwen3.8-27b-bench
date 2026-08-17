"""SQLite persistence.

Two tables:

  items   — append-only archive of every item ever fetched from every feed.
             Useful for auditing and debugging ("did this story ever come
             through?"). Nothing in the normal send path reads from it.

  sent    — the deduplication log.  Before sending a digest we check this
             table; after a successful send we write to it.  Keyed on
             (channel, source, link) so that:

             * Each channel keeps its own sent history — an item not yet
               delivered to "ops" can still be new to "ops" even if "energy"
               already sent it.

             * We use `link` as the stable identity key for all three feed
               formats:
                 - newsroom  has a stable raw_id (entry_id), but link is
                   equally stable and avoids a special-case branch.
                 - blogroll  has NO raw_id at all (None every time), so link
                   is the only viable key.
                 - wire/generic  has a guid that the provider regenerates on
                   every edit, making it useless for deduplication; link is
                   stable across edits.

             Using link uniformly keeps the logic simple and correct for all
             three providers.
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
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,
    source      TEXT NOT NULL,
    link        TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Fast existence check used on every cron run.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sent_key ON sent(channel, source, link);
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


def filter_unsent(conn, channel, items):
    """Return only the items from *items* that have not yet been sent to *channel*.

    Queries the sent table in a single IN-clause rather than N round-trips.
    """
    if not items:
        return []

    # Build a set of (source, link) pairs already delivered to this channel.
    links = [(i["source"], i["link"]) for i in items]
    placeholders = ",".join("(?,?)" for _ in links)
    flat_params = [v for pair in links for v in pair] 
    rows = conn.execute(
        f"SELECT source, link FROM sent WHERE channel = ?"
        f" AND (source, link) IN (VALUES {placeholders})",
        [channel, *flat_params],
    ).fetchall()
    already_sent = {(r["source"], r["link"]) for r in rows}

    return [i for i in items if (i["source"], i["link"]) not in already_sent]


def mark_sent(conn, channel, items):
    """Record that *items* were successfully delivered to *channel*.

    Uses INSERT OR IGNORE so a duplicate (e.g. from a crashed run that
    re-delivers before writing) is silently skipped.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, source, link) VALUES (?, ?, ?)",
        [(channel, i["source"], i["link"]) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
