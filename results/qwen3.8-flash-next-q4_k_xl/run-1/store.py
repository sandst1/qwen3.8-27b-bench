"""SQLite persistence.

Tables:

  * `items` is the archive of everything we have ever seen, mostly so that
    we can answer "did this ever come through?" when someone asks. Each row
    carries a `dedup_key` (see identity.py) with a unique index, so an item
    seen twice is stored once.

  * `deliveries` is the send ledger: one row per (channel, dedup_key). It
    is what stops people receiving the same item on every 15-minute cron
    tick. We track per channel rather than globally because channels filter
    differently — an item is "new" for a channel until *that channel* has
    received it. Rows are inserted only after a successful send, so a
    failed webhook delivery is retried on the next run.
"""

import sqlite3

import identity

TABLES = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    raw_id      TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    dedup_key   TEXT
);

CREATE TABLE IF NOT EXISTS deliveries (
    channel   TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, dedup_key)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Set during a legacy upgrade, consumed by digest.py once it knows the
# configured channel names.
SEED_PENDING_KEY = "seed_deliveries_from_archive"

INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_dedup ON items(dedup_key);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(TABLES)
    _migrate_add_column(conn)
    # Indexes before the backfill: UPDATE OR IGNORE in _migrate_backfill
    # relies on the unique index to skip duplicate rows.
    conn.executescript(INDEXES)
    _migrate_backfill(conn)
    conn.commit()
    return conn


def _migrate_add_column(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "dedup_key" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN dedup_key TEXT")
        # The old code sent every feed item to every channel on every run,
        # so anything already in the archive has been delivered. Flag the
        # DB so digest.py can seed the ledger and the upgrade does not
        # re-blast the whole archive on its first tick.
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, datetime('now'))",
            (SEED_PENDING_KEY,),
        )


def needs_delivery_seed(conn):
    return conn.execute(
        "SELECT 1 FROM meta WHERE key = ?", (SEED_PENDING_KEY,)
    ).fetchone() is not None


def seed_deliveries(conn, channels):
    """Mark everything in the archive as already delivered to `channels`."""
    for channel in channels:
        conn.execute(
            "INSERT OR IGNORE INTO deliveries (channel, dedup_key)"
            " SELECT ?, dedup_key FROM items WHERE dedup_key IS NOT NULL",
            (channel,),
        )
    conn.execute("DELETE FROM meta WHERE key = ?", (SEED_PENDING_KEY,))
    conn.commit()


def _migrate_backfill(conn):
    """Assign dedup keys to rows written before dedup existed.

    The old code re-inserted every item on every run, so legacy archives
    contain exact duplicates. UPDATE OR IGNORE gives the key to the oldest
    row per item and leaves the repeats with a NULL key (NULLs do not
    collide in a SQLite unique index).
    """
    legacy = conn.execute(
        "SELECT id, source, raw_id, title, link, published FROM items"
        " WHERE dedup_key IS NULL ORDER BY id"
    ).fetchall()
    for row in legacy:
        key = identity.item_key(dict(row))
        conn.execute(
            "UPDATE OR IGNORE items SET dedup_key = ? WHERE id = ?",
            (key, row["id"]),
        )


def record_items(conn, source, items):
    conn.executemany(
        "INSERT OR IGNORE INTO items"
        " (source, raw_id, title, link, summary, published, dedup_key)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                source,
                i["raw_id"],
                i["title"],
                i["link"],
                i.get("summary"),
                i.get("published"),
                i["key"],
            )
            for i in items
        ],
    )
    conn.commit()


def unseen_keys(conn, channel, keys):
    """The subset of `keys` this channel has not been sent yet, in order."""
    keys = list(dict.fromkeys(keys))
    if not keys:
        return []
    placeholders = ",".join("?" * len(keys))
    sent = {
        row["dedup_key"]
        for row in conn.execute(
            f"SELECT dedup_key FROM deliveries"
            f" WHERE channel = ? AND dedup_key IN ({placeholders})",
            (channel, *keys),
        )
    }
    return [k for k in keys if k not in sent]


def mark_delivered(conn, channel, keys):
    conn.executemany(
        "INSERT OR IGNORE INTO deliveries (channel, dedup_key) VALUES (?, ?)",
        [(channel, k) for k in keys],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
