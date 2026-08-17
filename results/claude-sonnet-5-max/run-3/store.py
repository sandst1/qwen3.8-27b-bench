"""SQLite persistence.

Two tables, two different jobs - don't merge them:

`items` is an archive of everything we have ever fetched, mostly so that
we can answer "did this ever come through?" when someone asks. Nothing
reads it at the moment. It is written to unconditionally on every fetch
(no dedup) so it does grow steadily; that's a pre-existing, separate
tradeoff from the delivery-dedup problem this module also solves, and is
left alone here.

`deliveries` is the ledger that actually prevents re-notifying people: one
row per (channel, dedupe_key) that has been sent. It is keyed per-channel,
not globally, because the same item can legitimately be sent to more than
one channel (e.g. a story matching both a narrow keyword channel and the
keywords-less "everything" firehose) - each channel should still get it
exactly once, on the first run where it matches that channel's filter.
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

CREATE TABLE IF NOT EXISTS deliveries (
    channel     TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    first_sent  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, dedupe_key)
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
        "INSERT INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in items
        ],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


def filter_unsent(conn, channel, items):
    """Return the subset of `items` not yet delivered to `channel`.

    Callers should pass this the items that already matched the channel's
    keyword filter; this only handles the "have we sent this one before"
    half of the decision.
    """
    if not items:
        return []
    keys = [i["dedupe_key"] for i in items]
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT dedupe_key FROM deliveries WHERE channel = ? AND dedupe_key IN ({placeholders})",
        [channel, *keys],
    )
    already_sent = {r["dedupe_key"] for r in rows}
    return [i for i in items if i["dedupe_key"] not in already_sent]


def record_deliveries(conn, channel, items):
    """Mark `items` as delivered to `channel` so they are not sent again."""
    if not items:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO deliveries (channel, dedupe_key) VALUES (?, ?)",
        [(channel, i["dedupe_key"]) for i in items],
    )
    conn.commit()
