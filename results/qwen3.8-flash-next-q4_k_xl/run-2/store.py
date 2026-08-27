"""SQLite persistence.

Two tables, two different jobs:

  items      An archive of every *distinct* article we have ever seen, so we
             can answer "did this ever come through?". Deduplicated on
             (source, dedup_key) so the archive stops growing every 15 minutes
             and stays a list of unique articles rather than one row per poll.

  delivered  The delivery ledger: one row per (channel, source, dedup_key) we
             have successfully sent. This is what stops people receiving the
             same item on every cron tick. It is tracked per channel, not
             globally, so that a channel only ever sees each article once but a
             newly added channel still gets a first-time backfill of whatever it
             cares about. Rows are written only AFTER a send succeeds, so a
             channel that is down simply retries on the next tick (see the
             best-effort note in channels.py).

`dedup_key` comes from feeds.item_key(); see that function for why the key is
the tracking-stripped link rather than raw_id or the raw link.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    raw_id      TEXT,
    dedup_key   TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

CREATE TABLE IF NOT EXISTS delivered (
    channel       TEXT NOT NULL,
    source        TEXT NOT NULL,
    dedup_key     TEXT NOT NULL,
    title         TEXT,
    delivered_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, dedup_key)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    """Bring an `items` table created before dedup_key existed up to date.

    The unique index is created here (not in SCHEMA) so that it only runs once
    the column is guaranteed to exist. Pre-existing rows have NULL dedup_key,
    and SQLite treats NULLs as distinct in a unique index, so adding the index
    never fails on an existing database.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "dedup_key" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN dedup_key TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_items_dedup"
        " ON items(source, dedup_key)"
    )


def record_items(conn, source, items):
    """Archive items, ignoring ones we already have. INSERT OR IGNORE relies
    on idx_items_dedup, so callers must set item["dedup_key"] first."""
    conn.executemany(
        "INSERT OR IGNORE INTO items (source, raw_id, dedup_key, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                source,
                i["raw_id"],
                i.get("dedup_key"),
                i["title"],
                i["link"],
                i.get("summary"),
                i.get("published"),
            )
            for i in items
        ],
    )
    conn.commit()


def delivered_keys(conn, channel):
    """Set of (source, dedup_key) already sent to this channel."""
    rows = conn.execute(
        "SELECT source, dedup_key FROM delivered WHERE channel = ?", (channel,)
    )
    return {(r["source"], r["dedup_key"]) for r in rows}


def mark_delivered(conn, channel, items):
    """Record a successful send. Call only after channels.send() returns."""
    conn.executemany(
        "INSERT OR IGNORE INTO delivered (channel, source, dedup_key, title)"
        " VALUES (?, ?, ?, ?)",
        [(channel, i["source"], i["dedup_key"], i["title"]) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
