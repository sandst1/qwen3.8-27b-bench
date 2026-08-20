"""SQLite persistence.

Two tables, with different jobs:

`items` is an archive of everything we have ever seen, so we can answer "did
this ever come through?". It is not consulted when deciding what to send.

`sent` is the delivery ledger and *is* load-bearing: it records that a given
item was successfully delivered to a given channel. Without it the job re-sends
its entire window every time cron fires.

The ledger is keyed on (channel, identity), not on identity alone. Channels
overlap on purpose — in the sample config both `ops` and `everything` match the
port-fee story — so a global "have we sent this?" would let whichever channel
ran first swallow the item and starve the others.

See `feeds.identity()` for what counts as the same item.
"""

import sqlite3

import feeds

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    raw_id      TEXT,
    identity    TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

-- One row per successful delivery of an item to a channel.
CREATE TABLE IF NOT EXISTS sent (
    channel     TEXT NOT NULL,
    identity    TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, identity)
) WITHOUT ROWID;
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    """Bring a pre-dedup database up to the current schema.

    Databases created before the ledger existed have an `items` table with no
    `identity` column and, because every poll inserted unconditionally, one row
    per item *per 15-minute tick*. Backfill the key, collapse those duplicates
    keeping the earliest sighting, then enforce uniqueness going forward.
    """
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    if "identity" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN identity TEXT")

    rows = conn.execute(
        "SELECT id, source, link, raw_id, title FROM items WHERE identity IS NULL"
    ).fetchall()
    if rows:
        conn.executemany(
            "UPDATE items SET identity = ? WHERE id = ?",
            [(feeds.identity(dict(r)), r["id"]) for r in rows],
        )

    # Collapse historical duplicates, keeping the row with the earliest
    # first_seen so the archive still answers "when did we first see this?".
    conn.execute(
        """
        DELETE FROM items WHERE id NOT IN (
            SELECT MIN(id) FROM items GROUP BY source, identity
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_items_identity"
        " ON items(source, identity)"
    )


def record_items(conn, source, items):
    """Archive newly seen items.

    `INSERT OR IGNORE` keeps `first_seen` meaning "first seen", and stops the
    archive growing by one row per item per tick forever. This is bookkeeping
    only — it has no say in what gets delivered.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO items"
        " (source, raw_id, identity, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                source,
                i["raw_id"],
                i["identity"],
                i["title"],
                i["link"],
                i.get("summary"),
                i.get("published"),
            )
            for i in items
        ],
    )
    conn.commit()


def filter_unsent(conn, channel, items):
    """Return the items not yet delivered to `channel`, order preserved.

    Also drops repeats *within* this batch: two feeds can carry the same story
    at the same url, and the reader should not see it twice in one digest.
    """
    if not items:
        return []
    already = {
        r["identity"]
        for r in conn.execute(
            "SELECT identity FROM sent WHERE channel = ?", (channel,)
        )
    }
    out = []
    for item in items:
        key = item["identity"]
        if key in already:
            continue
        already.add(key)
        out.append(item)
    return out


def mark_sent(conn, channel, items):
    """Record delivery. Call only *after* the channel accepted the digest."""
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, identity) VALUES (?, ?)",
        [(channel, i["identity"]) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


def count_sent(conn, channel=None):
    if channel is None:
        return conn.execute("SELECT COUNT(*) AS n FROM sent").fetchone()["n"]
    return conn.execute(
        "SELECT COUNT(*) AS n FROM sent WHERE channel = ?", (channel,)
    ).fetchone()["n"]
