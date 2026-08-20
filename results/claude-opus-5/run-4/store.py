"""SQLite persistence.

Two tables, with quite different jobs:

`items` is an archive of everything we have ever seen, so we can answer "did
this ever come through?" when someone asks. Nothing else reads it.

`deliveries` is load-bearing: it records that a given item was sent to a given
channel, and is what stops the 15-minute cron re-sending the same digest
forever. It is keyed per *channel*, not globally, because one item legitimately
goes to several channels -- suppressing an item everywhere just because the
"ops" channel already got it would silently starve the "everything" firehose.

Both tables key on (source, dedupe_key). The key is scoped to its source
because feeds.py may derive it from a provider id, and an id like "84121" is
only meaningful inside the feed that issued it.
"""

import sqlite3

# How long a delivery record is kept. This is not just housekeeping: an item
# that is still present in a feed after its record is pruned will be treated as
# new and re-sent. So this must stay comfortably longer than the longest window
# any feed keeps an item visible for. Generous on purpose -- the table gains a
# row per new item per channel, which is tens of rows a day, not millions.
DELIVERY_RETENTION_DAYS = 90

# Keys looked up per SELECT; each one binds two parameters, and SQLite's
# parameter cap is 999 on older builds.
_MAX_KEYS_PER_QUERY = 400

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    raw_id      TEXT,
    dedupe_key  TEXT NOT NULL,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_identity
    ON items(source, dedupe_key);

CREATE TABLE IF NOT EXISTS deliveries (
    channel     TEXT NOT NULL,
    source      TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_sent_at ON deliveries(sent_at);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _migrate(conn):
    """Bring a pre-dedupe database up to the current schema.

    Databases created before deduplication existed have no `dedupe_key` and,
    because every cron tick re-inserted the same rows, a pile of duplicate
    archive rows that would stop the new UNIQUE index from being built.
    """
    tables = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "items" not in tables:
        return  # Fresh database; SCHEMA creates everything.

    columns = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    if "dedupe_key" in columns:
        return  # Already migrated.

    conn.execute("ALTER TABLE items ADD COLUMN dedupe_key TEXT")
    # Backfill with the same shape feeds.py emits for link-keyed items. Old
    # rows are archive-only, so an approximate key is fine here; it is never
    # consulted to decide whether to send anything.
    conn.execute(
        "UPDATE items SET dedupe_key = 'link:' || link WHERE dedupe_key IS NULL"
    )
    # Collapse the duplicates the old code accumulated, keeping the earliest
    # row of each group so `first_seen` stays truthful.
    conn.execute(
        "DELETE FROM items WHERE id NOT IN ("
        "  SELECT MIN(id) FROM items GROUP BY source, dedupe_key"
        ")"
    )
    conn.commit()


def record_items(conn, source, items):
    """Add items to the archive, ignoring ones already recorded.

    OR IGNORE rather than plain INSERT so that re-seeing an item on the next
    poll does not add another row and does not disturb its `first_seen`.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO items"
        " (source, raw_id, dedupe_key, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                source,
                i["raw_id"],
                i["dedupe_key"],
                i["title"],
                i["link"],
                i.get("summary"),
                i.get("published"),
            )
            for i in items
        ],
    )
    conn.commit()


def already_delivered(conn, channel, items):
    """Return the {(source, dedupe_key)} of `items` already sent to `channel`."""
    if not items:
        return set()
    keys = {(i["source"], i["dedupe_key"]) for i in items}
    found = set()
    # Chunked because each key binds two parameters and SQLite caps the number
    # of parameters per statement (999 on older builds). Our feeds are small
    # today, but a chatty one shouldn't turn into a query error.
    keys = list(keys)
    for start in range(0, len(keys), _MAX_KEYS_PER_QUERY):
        chunk = keys[start : start + _MAX_KEYS_PER_QUERY]
        placeholders = ",".join(["(?, ?)"] * len(chunk))
        rows = conn.execute(
            "SELECT source, dedupe_key FROM deliveries"
            f" WHERE channel = ? AND (source, dedupe_key) IN ({placeholders})",
            [channel, *[part for key in chunk for part in key]],
        )
        found.update((r["source"], r["dedupe_key"]) for r in rows)
    return found


def record_delivery(conn, channel, items):
    """Mark `items` as delivered to `channel`.

    Call this only after the send succeeded -- see the note in digest.py about
    why we accept a rare repeat over a silent drop.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO deliveries (channel, source, dedupe_key)"
        " VALUES (?, ?, ?)",
        [(channel, i["source"], i["dedupe_key"]) for i in items],
    )
    conn.commit()


def prune_deliveries(conn, retention_days=DELIVERY_RETENTION_DAYS):
    """Drop delivery records older than the retention window."""
    cur = conn.execute(
        "DELETE FROM deliveries WHERE sent_at < datetime('now', ?)",
        (f"-{int(retention_days)} days",),
    )
    conn.commit()
    return cur.rowcount


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
