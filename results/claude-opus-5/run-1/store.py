"""SQLite persistence.

Two tables, with different jobs:

`items` is an archive of everything we have ever seen, so that we can answer
"did this ever come through?" when someone asks. Nothing else reads it.

`deliveries` is the one that matters operationally: it records which item has
been sent to which channel, and is what stops the 15-minute cron tick from
resending the same digest forever.

## Why deliveries is keyed per channel

A given item can match several channels. If we recorded delivery globally,
the first channel to receive an item would suppress it for every other
channel, and those channels would silently never see it. So the ledger is
keyed (channel, item_key) — each channel gets its own independent view.

## Ordering, and why duplicates beat silence

`mark_delivered` is called *after* the channel send returns successfully.
That ordering is deliberate:

  * mark-then-send would lose items permanently whenever a webhook is down,
    because the ledger would claim they were delivered.
  * send-then-mark can, if we die between the two, resend one digest.

That makes delivery at-least-once. For a digest, an occasional repeat after a
crash is a much cheaper failure than an item nobody ever sees.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    item_key    TEXT NOT NULL,
    raw_id      TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

-- Makes first_seen mean what it says: without this the cron job inserted a
-- fresh row for every item on every tick (96 copies a day, each claiming to
-- be the first sighting).
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_key ON items(item_key);

CREATE TABLE IF NOT EXISTS deliveries (
    channel      TEXT NOT NULL,
    item_key     TEXT NOT NULL,
    delivered_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, item_key)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _migrate(conn):
    """Bring a pre-deduplication database up to the current schema.

    The deployed box has a live digest.sqlite3 whose `items` table has no
    item_key column and is full of duplicate rows, so a plain CREATE TABLE
    IF NOT EXISTS would leave it broken and the unique index would fail to
    build. Rebuilding is safe: nothing reads this table.
    """
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "items" not in tables:
        return
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    if "item_key" in columns:
        return

    # Old rows predate stable keys. We cannot recompute them faithfully here
    # (the per-format rules live in feeds.py and need the original payload),
    # so collapse duplicates on the fields we do have and keep the earliest
    # sighting, tagging them ':legacy:' to make their provenance obvious.
    #
    # Consequence, accepted knowingly: these legacy rows will not match the
    # keys feeds.py computes from now on, so each still-live item gets one
    # additional archive row on the next run. The archive is write-only
    # ("did this ever come through?"), so a handful of one-off duplicate
    # rows is not worth a payload-reparsing migration to avoid.
    conn.executescript(
        """
        ALTER TABLE items RENAME TO items_old;
        CREATE TABLE items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            item_key    TEXT NOT NULL,
            raw_id      TEXT,
            title       TEXT NOT NULL,
            link        TEXT NOT NULL,
            summary     TEXT,
            published   TEXT,
            first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO items (source, item_key, raw_id, title, link, summary,
                           published, first_seen)
            SELECT source,
                   source || ':legacy:' || link,
                   MIN(raw_id), MIN(title), link, MIN(summary),
                   MIN(published), MIN(first_seen)
            FROM items_old
            GROUP BY source, link;
        DROP TABLE items_old;
        """
    )
    conn.commit()


def record_items(conn, source, items):
    """Add newly seen items to the archive. Repeat sightings are ignored."""
    conn.executemany(
        "INSERT OR IGNORE INTO items"
        " (source, item_key, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                source,
                i["key"],
                i.get("raw_id"),
                i["title"],
                i["link"],
                i.get("summary"),
                i.get("published"),
            )
            for i in items
        ],
    )
    conn.commit()


def delivered_keys(conn, channel, keys):
    """Return the subset of `keys` already delivered to `channel`."""
    keys = list(keys)
    if not keys:
        return set()
    found = set()
    # Chunked to stay under SQLite's variable limit on large feeds.
    for start in range(0, len(keys), 500):
        chunk = keys[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT item_key FROM deliveries"
            f" WHERE channel = ? AND item_key IN ({placeholders})",
            [channel, *chunk],
        )
        found.update(r["item_key"] for r in rows)
    return found


def mark_delivered(conn, channel, keys):
    """Record that `keys` reached `channel`. Call only after a successful send."""
    conn.executemany(
        "INSERT OR IGNORE INTO deliveries (channel, item_key) VALUES (?, ?)",
        [(channel, k) for k in keys],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


def count_deliveries(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"]
