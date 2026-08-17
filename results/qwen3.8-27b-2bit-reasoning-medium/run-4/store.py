"""SQLite persistence and deduplication.

The `items` table records every item we have ever seen. It is the source of
truth for "have we already sent this?", so `digest.run_once` only forwards
items that are new to this table. That is what stops the cron job from re-
sending the same items every 15 minutes.

Identity
--------
An item is identified by (source, dedup_key), where `dedup_key` is the item's
link with its query string and fragment stripped (see `item_key`). We use the
link rather than the provider's id because the link is the only field that is
stable across all of our feeds:

  * `blogroll` has no id at all — a permalink is all it gives us.
  * the `wire` feed regenerates its guid whenever an item is edited (typos,
    retitles), so the guid is *not* a reliable identity; the link is.
  * the `newsroom` feed tacks rotating tracking params (utm_*) onto its URLs,
    so we strip the query string to get a stable key.

Consequences of that choice:

  * An item that is *edited* (new guid / new title, same link) is the same
    item and is not re-sent.
  * If a feed ever reuses one link for genuinely different content, that
    content will be collapsed into one item. If that becomes a problem, change
    `item_key` (e.g. to include the provider id for feeds whose ids are
    stable).

Migration
---------
Databases created before dedup existed have no `dedup_key` column and contain
duplicate rows (the old code re-inserted everything every run). `connect()`
adds the column, backfills it, collapses those duplicates, and creates the
unique index. This is idempotent; it is a no-op on fresh databases.
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
    dedup_key   TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
"""


def _normalize_link(link):
    """Strip query + fragment: providers append rotating tracking params
    (utm_*) that would otherwise make the same item look new every run."""
    parts = urlsplit(link)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def item_key(item):
    """Stable identity for an item: its link, normalised (see module doc)."""
    link = item.get("link") or ""
    if link:
        return _normalize_link(link)
    # Defensive: the normalised shape always carries a link, but if one is
    # ever missing, fall back to whatever else identifies the item.
    return (item.get("raw_id") or item.get("title") or "").strip()


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    if "dedup_key" not in cols:
        # Pre-dedup database: add the column, backfill it from the stored
        # link, then collapse the duplicates the old code accumulated (keep
        # the earliest row per identity, so first_seen stays correct).
        conn.execute("ALTER TABLE items ADD COLUMN dedup_key TEXT")
        for row in conn.execute("SELECT id, link, raw_id, title FROM items"):
            key = item_key(
                {"link": row["link"], "raw_id": row["raw_id"], "title": row["title"]}
            )
            conn.execute("UPDATE items SET dedup_key = ? WHERE id = ?", (key, row["id"]))
        conn.execute(
            "DELETE FROM items WHERE id NOT IN ("
            "SELECT MIN(id) FROM items GROUP BY source, dedup_key)"
        )
    # Only created after dedup above, so the unique constraint holds even on
    # an old database that still has duplicate rows.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_dedup ON items(source, dedup_key)")
    conn.commit()


def record_items(conn, source, items, dry_run=False):
    """Record items and return the ones we have not seen before.

    An item is "seen" once its (source, dedup_key) is in the table. With
    ``dry_run`` we only check and do not insert, so a preview does not
    consume items (the next real run still sends them).
    """
    new_items = []
    for item in items:
        key = item_key(item)
        seen = conn.execute(
            "SELECT 1 FROM items WHERE source = ? AND dedup_key = ?",
            (source, key),
        ).fetchone()
        if seen is not None:
            continue
        new_items.append(item)
        if not dry_run:
            conn.execute(
                "INSERT INTO items (source, raw_id, title, link, summary, published, dedup_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    source,
                    item.get("raw_id"),
                    item["title"],
                    item["link"],
                    item.get("summary"),
                    item.get("published"),
                    key,
                ),
            )
    if not dry_run:
        conn.commit()
    return new_items


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
