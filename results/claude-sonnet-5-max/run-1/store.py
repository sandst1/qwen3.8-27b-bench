"""SQLite persistence.

The `items` table has a dual role:

1. An archive of everything we've ever recorded (source, title, link, ...),
   for "did this ever come through?" questions.
2. The dedup ledger digest.run_once checks on every tick via
   `filter_unseen`, keyed on `dedupe_key` (see feeds.py for how that's
   derived -- it is *not* simply raw_id). This is what stops the same item
   being sent again on every 15-minute cron run just because it's still
   sitting in the feed's current window: previously this table was written
   to but never read back, so nothing ever short-circuited a re-send.

The UNIQUE index on dedupe_key is what makes `record_items` idempotent:
re-recording an item we already know about is a harmless no-op instead of
a duplicate row or an error.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    dedupe_key  TEXT,
    raw_id      TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
"""


def _ensure_dedupe_key_column(conn):
    """Add dedupe_key to a database that predates it.

    `CREATE TABLE IF NOT EXISTS` above is a no-op against a table that
    already exists (e.g. the digest.sqlite3 already deployed from cron),
    so a plain schema bump wouldn't actually add the column there -- the
    next INSERT/SELECT referencing dedupe_key would just fail with "no
    such column". This is the migration for that case.

    Pre-existing rows are left with dedupe_key = NULL: we have no reliable
    way to recompute it after the fact (that needs the feed *format*,
    which was never stored). SQLite treats every NULL as distinct under a
    UNIQUE index, so this is safe -- it just means whatever was already in
    the archive doesn't dedupe against future fetches. Practically, that's
    at most one extra re-send, right after upgrading, for items that were
    already in flight; every run after that is deduped normally.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "dedupe_key" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN dedupe_key TEXT")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _ensure_dedupe_key_column(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_items_dedupe_key ON items(dedupe_key)"
    )
    conn.commit()
    return conn


def filter_unseen(conn, items):
    """Return the items in `items` we have not already recorded.

    Compares on dedupe_key, not raw_id/link individually -- see feeds.py
    for why. This is the actual dedup gate: call it before deciding what
    goes into a digest, not after.
    """
    if not items:
        return []
    keys = list({i["dedupe_key"] for i in items})
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT dedupe_key FROM items WHERE dedupe_key IN ({placeholders})",
        keys,
    )
    known = {row["dedupe_key"] for row in rows}
    return [i for i in items if i["dedupe_key"] not in known]


def record_items(conn, items):
    """Record `items` as seen.

    Uses INSERT OR IGNORE: `items` may include ones already in the archive
    (digest.run_once currently only calls this with the unseen subset, but
    this function stays safe to call with anything), and the UNIQUE index
    on dedupe_key would otherwise turn that into a constraint error instead
    of a no-op.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO items"
        " (source, dedupe_key, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                i["source"],
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


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
