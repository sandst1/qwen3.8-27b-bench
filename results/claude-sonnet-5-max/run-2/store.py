"""SQLite persistence.

The `items` table is an archive of everything we have ever seen, one row per
distinct item (see feeds.dedupe_key for what "distinct" means -- it's not
the same as the provider's raw_id). Re-fetching a feed re-lists items it has
shown before as well as new ones, so record_items() is keyed on dedupe_key
and uses INSERT OR IGNORE to avoid piling up duplicate rows every run.

The `deliveries` table is what actually stops people from being re-notified:
it remembers, per channel, which dedupe_keys have already been sent so a
digest only ever contains items that channel hasn't seen before. It's keyed
on (channel, dedupe_key) rather than dedupe_key alone because the same item
can legitimately go to more than one channel (keyword filters overlap) --
each channel needs to see it once, independently of the others.
"""

import sqlite3

import feeds

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    raw_id      TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_items_dedupe_key ON items(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

CREATE TABLE IF NOT EXISTS deliveries (
    channel     TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, dedupe_key)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _migrate_legacy_items(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _migrate_legacy_items(conn):
    """Upgrade an `items` table from before dedupe_key existed, in place.

    This cron job already runs in production against a live db file, and
    the whole reason we're adding dedupe_key is that the old code had no
    dedup at all -- so a real `items` table here almost certainly has many
    duplicate rows for the same underlying item (one per cron tick it
    happened to still be in the feed). Without this, deploying the fix
    would crash every run with "no such column: dedupe_key" the moment
    SCHEMA below tries to use it, or (once the column existed) fail to
    build the unique index over pre-existing duplicates.

    So: add the column, backfill it from source+link, then collapse
    duplicates down to the earliest sighting, *before* SCHEMA creates the
    unique index. A no-op on a fresh db (no `items` table yet) or one
    that's already been migrated.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if not cols or "dedupe_key" in cols:
        return

    conn.execute("ALTER TABLE items ADD COLUMN dedupe_key TEXT")
    rows = conn.execute("SELECT id, source, link FROM items").fetchall()
    conn.executemany(
        "UPDATE items SET dedupe_key = ? WHERE id = ?",
        [(feeds.dedupe_key(row["source"], {"link": row["link"]}), row["id"]) for row in rows],
    )
    conn.execute(
        "DELETE FROM items WHERE id NOT IN"
        " (SELECT MIN(id) FROM items GROUP BY dedupe_key)"
    )
    conn.commit()


def record_items(conn, source, items):
    conn.executemany(
        "INSERT OR IGNORE INTO items"
        " (source, dedupe_key, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                source,
                i["dedupe_key"],
                i["raw_id"],
                i["title"],
                i["link"],
                i.get("summary"),
                i.get("published"),
            )
            for i in items
        ],
    )
    conn.commit()


def unsent(conn, channel, items):
    """Filter `items` (already matched to `channel`) down to the ones we
    have never delivered to that channel before. Order is preserved."""
    if not items:
        return []
    keys = [i["dedupe_key"] for i in items]
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT dedupe_key FROM deliveries"
        f" WHERE channel = ? AND dedupe_key IN ({placeholders})",
        [channel, *keys],
    ).fetchall()
    already_sent = {row["dedupe_key"] for row in rows}
    return [i for i in items if i["dedupe_key"] not in already_sent]


def mark_sent(conn, channel, items):
    """Record that `items` were just delivered to `channel`, so unsent()
    excludes them next time. Call this only after delivery actually
    succeeds -- see channels.send / digest.run_once."""
    conn.executemany(
        "INSERT OR IGNORE INTO deliveries (channel, dedupe_key) VALUES (?, ?)",
        [(channel, i["dedupe_key"]) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
