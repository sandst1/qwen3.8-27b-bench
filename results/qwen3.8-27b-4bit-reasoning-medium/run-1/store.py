"""SQLite persistence.

Two tables:

`items` — one row per unique item we have ever seen (deduped by
`identity`), kept so we can answer "did this ever come through?".

`sent` — the delivery ledger: one row per (item, channel) pair that has
been delivered successfully. This is what stops the cron job from
re-sending the same items every 15 minutes. An item is only marked sent
*after* the channel accepts the digest, so a failed delivery is retried on
the next tick while channels that already got the item are not spammed.

`identity` is chosen per feed format in feeds.py. Do not derive it from
raw_id here: raw_id is not always stable (see the feeds.py docstring).

Schema changes go through _migrate(), tracked in schema_version.
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
    identity    TEXT NOT NULL,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

CREATE TABLE IF NOT EXISTS sent (
    identity  TEXT NOT NULL,
    channel   TEXT NOT NULL,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (identity, channel)
);

CREATE TABLE IF NOT EXISTS schema_version (
    version   INTEGER NOT NULL
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
    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_version"
    ).fetchone()[0]
    if version >= 1:
        return

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "identity" not in cols:
        # Pre-identity database. Backfill the way feeds.py would have
        # derived identity: the provider's id when that one was stable,
        # otherwise the link. Legacy `generic` rows were keyed by guid,
        # which we no longer trust, so those items are re-archived (and
        # re-sent exactly once) under their link-based identity.
        conn.execute("ALTER TABLE items ADD COLUMN identity TEXT")
        conn.execute(
            "UPDATE items SET identity = source || ':' || COALESCE(raw_id, link)"
        )

    # Old runs inserted every item on every tick, so the pre-migration
    # table is full of duplicates; collapse to the earliest row per
    # identity before the unique index can exist.
    conn.execute(
        "DELETE FROM items WHERE id NOT IN"
        " (SELECT MIN(id) FROM items GROUP BY identity)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_items_identity ON items(identity)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")


def record_items(conn, source, items):
    # INSERT OR IGNORE: the archive keeps one row per identity.
    conn.executemany(
        "INSERT OR IGNORE INTO items"
        " (source, raw_id, title, link, summary, published, identity)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                source,
                i["raw_id"],
                i["title"],
                i["link"],
                i.get("summary"),
                i.get("published"),
                i["identity"],
            )
            for i in items
        ],
    )
    conn.commit()


def sent_identities(conn, channel):
    """Identities already delivered to `channel`."""
    rows = conn.execute(
        "SELECT identity FROM sent WHERE channel = ?", (channel,)
    ).fetchall()
    return {row["identity"] for row in rows}


def mark_sent(conn, channel, identities):
    """Call only after the channel has accepted the digest."""
    conn.executemany(
        "INSERT OR IGNORE INTO sent (identity, channel) VALUES (?, ?)",
        [(identity, channel) for identity in identities],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
