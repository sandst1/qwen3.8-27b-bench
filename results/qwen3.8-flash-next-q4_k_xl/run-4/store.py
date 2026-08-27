"""SQLite persistence.

Two responsibilities:

  * `items` is an archive of everything we have ever seen, so we can answer
    "did this ever come through?" when someone asks. It is keyed by
    `dedup_key` (see feeds.item_key) so re-fetching the same story does not
    add a second row.

  * `delivered` is the ledger that actually stops duplicate digests: one row
    per (channel, dedup_key) we have successfully sent. Dedup is deliberately
    per channel — a story that only matches `ops` must still be eligible for
    `energy` the first time it appears, and a channel that was down and
    retried must not lose its items.

Old databases (before dedup) are migrated in place by connect(); see
_migrate().
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_dedup ON items(dedup_key);

CREATE TABLE IF NOT EXISTS delivered (
    channel     TEXT NOT NULL,
    dedup_key   TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, dedup_key)
);
"""


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn):
    """Bring a pre-dedup database up to date. The deployed digest.sqlite3
    already has an `items` table without `dedup_key`, and CREATE TABLE
    IF NOT EXISTS will not add columns, so do it explicitly. Existing rows
    are left with a NULL key (they stay in the archive; they just won't be
    matched for dedup, which is fine because delivery tracking starts fresh).
    """
    cols = _columns(conn, "items")
    if cols and "dedup_key" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN dedup_key TEXT")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_items(conn, source, items):
    """Insert newly seen items into the archive. INSERT OR IGNORE keeps it a
    true 'everything ever seen, once' archive even though cron refetches the
    same stories every 15 minutes."""
    conn.executemany(
        "INSERT OR IGNORE INTO items"
        " (source, raw_id, dedup_key, title, link, summary, published)"
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
    """The set of dedup_keys already sent to this channel."""
    rows = conn.execute(
        "SELECT dedup_key FROM delivered WHERE channel = ?", (channel,)
    ).fetchall()
    return {r["dedup_key"] for r in rows}


def record_deliveries(conn, channel, keys):
    """Mark items as sent to a channel. Call only after a successful send so a
    failed channel is retried on the next tick (see channels.py)."""
    conn.executemany(
        "INSERT OR IGNORE INTO delivered (channel, dedup_key) VALUES (?, ?)",
        [(channel, k) for k in keys],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
