"""SQLite persistence.

Two tables, two jobs:

  items -- an archive of every distinct item we have ever seen, one row per
           item, keyed by link_key. Answers "did this ever come through?".
           Inserts are idempotent (INSERT OR IGNORE), so re-polling a feed
           that still lists an old story does not add rows.

  sent  -- the dedupe table that actually stops repeat deliveries. One row
           per (channel, link_key) that has been delivered. digest.py reads
           it before rendering and writes it after each successful send.
           It is intentionally never pruned: deleting a row would re-send
           that item to that channel on the next tick.

Databases created before the `sent` table existed are upgraded in place by
_migrate() on the next connect().
"""

import sqlite3
from urllib.parse import urlsplit, urlunsplit

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    link_key    TEXT NOT NULL,
    raw_id      TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_items_link_key ON items(link_key);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

CREATE TABLE IF NOT EXISTS sent (
    channel   TEXT NOT NULL,
    link_key  TEXT NOT NULL,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, link_key)
);
"""


def canonical_link(link):
    """Normalise a URL to a stable identity.

    Strips the query string and fragment and lower-cases scheme and host.
    The query string is the important part: newsroom rewrites its utm_*
    params on every snapshot, so the raw URL of the same story changes
    from one poll to the next.
    """
    parts = urlsplit((link or "").strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


def item_key(item):
    """Stable identity for one item, used to dedupe across cron runs.

    We deliberately key on the canonical link rather than the feed's own id,
    because no provider id is reliable:

      * newsroom rewrites the utm_* query params on every snapshot, so the
        raw URL (and any key built from it) changes for the same story;
      * blogroll has no identifier at all (raw_id is None);
      * wire regenerates its guid whenever an item is edited.

    The link minus its tracking params is the one thing all three providers
    keep stable for the same story, so an edited story (new title, new guid,
    new utm params) is still recognised as one we already sent.

    Falls back to raw_id, then title, for a hypothetical feed with no links.
    """
    link = item.get("link")
    if link:
        return canonical_link(link)
    if item.get("raw_id"):
        return "id:" + str(item["raw_id"])
    return "title:" + (item.get("title") or "")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _migrate(conn):
    """Upgrade databases created before dedupe existed.

    The old schema had no stable key and inserted one row per poll, so an
    old `items` table holds the same story once per 15 minutes. Add the
    link_key column, backfill it from the stored link, and collapse the
    duplicates (keeping the earliest row so first_seen survives) before the
    schema script creates the unique index.

    Note: the new `sent` table starts empty, so the first run after an
    upgrade re-sends whatever the feeds currently list, once. We do not
    backfill `sent` from `items` because the old code archived items before
    sending them, so some archived items may never have been delivered.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    if not cols or "link_key" in cols:
        return
    conn.execute("ALTER TABLE items ADD COLUMN link_key TEXT")
    # `id` is the INTEGER PRIMARY KEY, i.e. the rowid (SQLite aliases the
    # two), and is present in both the old and the new schema.
    rows = conn.execute("SELECT id, link FROM items").fetchall()
    conn.executemany(
        "UPDATE items SET link_key = ? WHERE id = ?",
        [(canonical_link(r["link"]), r["id"]) for r in rows],
    )
    conn.execute(
        "DELETE FROM items WHERE id NOT IN"
        " (SELECT MIN(id) FROM items GROUP BY link_key)"
    )
    conn.commit()


def record_items(conn, source, items):
    """Archive each distinct item once; a no-op for items already archived."""
    conn.executemany(
        "INSERT OR IGNORE INTO items"
        " (source, link_key, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                source,
                item_key(i),
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


def sent_keys(conn, channel):
    """Set of link_keys already delivered to `channel`."""
    return {
        r["link_key"]
        for r in conn.execute("SELECT link_key FROM sent WHERE channel = ?", (channel,))
    }


def mark_sent(conn, channel, keys):
    """Remember that `channel` received the items with these link_keys."""
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, link_key) VALUES (?, ?)",
        [(channel, k) for k in keys],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
