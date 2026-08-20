"""SQLite persistence.

The `items` table is an archive of everything we have ever seen, plus when each
item was first delivered. The delivery side of that table (`sent_at`) is what
stops cron from re-sending the same items on every 15-minute tick: a run only
selects items whose `sent_at` is still NULL, marks the ones it actually
delivered, and the next run skips them.

Identity is the subtle part — see `item_key`.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    key         TEXT NOT NULL,
    raw_id      TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    sent_at     TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def migrate(conn):
    """Bring an old database up to the current schema.

    A database created before de-duplication existed has an `items` table with
    no `key` or `sent_at` column. ALTER TABLE cannot add a NOT NULL/UNIQUE
    column, so we add both nullable and back uniqueness with an index; SQLite
    allows multiple NULLs in a unique index, which is exactly what the
    pre-existing, unkeyed rows are. Those old rows are simply re-evaluated once
    on the next run — nothing is sent twice afterwards.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    if "key" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN key TEXT")
    if "sent_at" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN sent_at TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_key ON items(key)")
    conn.commit()


def item_key(item):
    """A stable identity for an item that survives edits and re-publishes.

    The three feed formats disagree about what identifies an item:
      * newsroom has a stable entry id, but its URL carries a changing
        `utm_campaign` tag, so the link alone is not stable;
      * blogroll has no id at all — only a permalink;
      * the wire's guid is regenerated every time the item is edited.
    The one field they all agree on is the link, modulo tracking query params,
    so we key on the link with the query string and fragment stripped. The
    source is folded in so two feeds that happen to share a URL don't collide.
    """
    link = item.get("link", "")
    link = link.split("?", 1)[0].split("#", 1)[0]
    return f"{item.get('source', '')}\x1f{link}"


def record_items(conn, source, items):
    """Archive each item once, keyed by `item_key`.

    INSERT OR IGNORE keeps the archive deduplicated, so revisiting an item (an
    edit, a re-published guid) neither raises nor creates a second row.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO items (source, key, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (source, item_key(i), i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in items
        ],
    )
    conn.commit()


def sent_keys(conn):
    """Keys of everything that has been delivered at least once."""
    return {r["key"] for r in conn.execute(
        "SELECT key FROM items WHERE sent_at IS NOT NULL"
    )}


def mark_sent(conn, keys):
    """Record that these keys were delivered on this run."""
    keys = list(keys)
    if not keys:
        return
    conn.executemany(
        "UPDATE items SET sent_at = datetime('now') WHERE key = ?",
        [(k,) for k in keys],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
