"""SQLite persistence.

The `items` table serves two purposes:

  1. Dedup memory. Every item we have ever seen is recorded here, keyed by
     (source, link_key). `digest.py` only sends items whose key is not already
     in the table, which is what stops the cron job from re-sending the same
     items every 15 minutes.
  2. Archive. It also lets us answer "did this ever come through?" when
     someone asks.

How an item is identified
-------------------------
We dedup on a canonical link (`link_key`), *not* on the feed's own id. The
three providers do not agree on identifiers:

  * newsroom  -> stable `entry_id`, but its URLs carry rotating tracking
                 params (utm_campaign=w33, w34, ...) that change each poll.
  * blogroll  -> no identifier at all; the permalink is the only stable field.
  * wire      -> has a `guid`, but it is regenerated whenever an item is
                 edited (a typo fix or retile changes it).

The path (scheme + host + path, no query, no fragment) is the one thing that
is stable across all three, so that is the identity. See README, "How dedup
works", for the trade-offs.
"""

import sqlite3
from urllib.parse import urlsplit, urlunsplit

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    raw_id      TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    link_key    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
"""


def _link_key(link):
    """Canonical form of an item link; the stable identity used for dedup.

    We keep scheme + host + path and drop the query string and fragment.
    Dropping the query matters: newsroom appends rotating tracking params
    (utm_campaign=...) that change on every poll, so the full URL would look
    brand new each time and we would re-send everything.
    """
    parts = urlsplit(link or "")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    """Bring an old-digest DB (pre-`link_key`) up to the current schema.

    Old DBs have an `items` table with no `link_key` column and one row per
    *fetch* — very bloated, because the pre-fix code re-archived every item on
    every 15-minute run. We:
      1. add the `link_key` column,
      2. backfill it from the stored `link`,
      3. collapse duplicate rows (same source + link_key) to a single row,
         keeping the earliest `first_seen`,
      4. add the unique identity index.

    On a fresh DB the column already exists, so only step 4 runs (and the
    index already exists, so it is a no-op).
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    if "link_key" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN link_key TEXT")
        rows = conn.execute("SELECT id, link FROM items WHERE link_key IS NULL").fetchall()
        conn.executemany(
            "UPDATE items SET link_key = ? WHERE id = ?",
            [(_link_key(r["link"]), r["id"]) for r in rows],
        )
        conn.execute(
            "DELETE FROM items WHERE id NOT IN "
            "(SELECT MIN(id) FROM items GROUP BY source, link_key)"
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_items_identity ON items(source, link_key)"
    )


def unseen_items(conn, source, items):
    """Return the items from `items` not yet recorded for `source`.

    Duplicates within the batch are collapsed too (a feed listing the same
    link twice in one poll counts once).
    """
    if not items:
        return []
    keys = [_link_key(i["link"]) for i in items]
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT link_key FROM items WHERE source = ? AND link_key IN ({placeholders})",
        [source, *keys],
    ).fetchall()
    present = {r["link_key"] for r in rows}
    out = []
    seen = set()
    for item, key in zip(items, keys):
        if key in present or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def record_items(conn, source, items):
    """Record items in the archive. Idempotent: re-archiving a known item is a no-op."""
    if not items:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO items (source, raw_id, title, link, summary, published, link_key)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"), _link_key(i["link"]))
            for i in items
        ],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
