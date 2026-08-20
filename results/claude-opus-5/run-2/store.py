"""SQLite persistence.

Two tables, with different jobs:

`items` is an archive of everything we have ever seen, so that we can answer
"did this ever come through?" when someone asks. Nothing reads it in normal
operation.

`sent` is the ledger that stops the job re-sending the same item every time
cron fires. It is keyed on (channel, dedup_key) — per channel, because one
item can legitimately match several channels and marking it sent for the
`ops` digest must not silence it for the firehose. See feeds.py for how
`dedup_key` is derived and why it differs per provider.

The ledger is never pruned. At current volumes (a handful of items a day)
it stays small for years, and any retention window would re-send anything
that outlived it — a feed that keeps an old item parked at the top would
start repeating again. If it ever does need pruning, prune by `sent_at` and
pick a window comfortably longer than the longest time an item stays in a
feed.
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
    channel     TEXT NOT NULL,
    dedup_key   TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, dedup_key)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_items(conn, source, items):
    """Append to the seen-items archive.

    This is an archive, not the dedup ledger: it keeps one row per sighting,
    so an item edited upstream shows up more than once and you can see how it
    changed. Do not read it to decide what to send — use `unsent` for that.
    """
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in items
        ],
    )
    conn.commit()


def unsent(conn, channel, items):
    """Return the subset of `items` not yet delivered to `channel`, in order."""
    if not items:
        return []
    keys = [i["dedup_key"] for i in items]
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT dedup_key FROM sent WHERE channel = ? AND dedup_key IN ({placeholders})",
        [channel, *keys],
    ).fetchall()
    already = {r["dedup_key"] for r in rows}
    return [i for i in items if i["dedup_key"] not in already]


def mark_sent(conn, channel, items):
    """Record `items` as delivered to `channel`.

    Called only after the channel accepted the digest, so that a failed
    delivery is retried on the next cron tick rather than lost. The flip side
    is that a crash between sending and this commit re-sends that digest —
    the safe direction to fail in.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, dedup_key) VALUES (?, ?)",
        [(channel, i["dedup_key"]) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


def count_sent(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM sent").fetchone()["n"]
