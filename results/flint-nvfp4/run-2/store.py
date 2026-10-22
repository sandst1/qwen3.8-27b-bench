"""SQLite persistence.

The `items` table is an observation archive of everything we have ever seen.
The separate `deliveries` ledger is the delivery truth: one row per successfully
sent (channel, item). Runs only pick items that are still pending for a channel.
"""

import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


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
    source      TEXT NOT NULL,
    link        TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, link)
);
"""

_DELIVERED = "SELECT 1 FROM deliveries WHERE channel = ? AND source = ? AND link = ?"


def connect(path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def item_key(item):
    """Return the (source, normalized link) identity of an item.

    Feed providers rotate an item's ID whenever it is edited, so IDs are not a
    stable identity. The permalink is: strip tracking query parameters
    (``utm_*``) and fragments, sort the remaining query parameters, lowercase
    the scheme and host, and keep the path spelling and meaningful query
    parameters as they are. The original link is kept for rendering and in the
    archive; this value is only the comparison key.
    """
    parts = urlsplit(item.get("link") or "")
    pairs = sorted(
        (k.lower(), v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
    )
    normalized = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(pairs), ""))
    return (item.get("source"), normalized)


def record_items(conn, source, items):
    known = {
        (row["source"], item_key({"source": row["source"], "link": row["link"]})[1])
        for row in conn.execute("SELECT source, link FROM items")
    }
    fresh = [
        (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
        for i in items
        if (i["source"], item_key(i)[1]) not in known
    ]
    if fresh:
        conn.executemany(
            "INSERT INTO items (source, raw_id, title, link, summary, published) VALUES (?, ?, ?, ?, ?, ?)",
            fresh,
        )


def get_pending(conn, channel, items):
    """Return the items still awaiting delivery to a channel, in input order.

    An item is pending until it has been delivered successfully to the channel
    in question; deliveries are recorded per channel so independent channels
    can each get the items they care about. Within one batch, items sharing an
    identity are only ever sent once.
    """
    pending = []
    seen = set()
    for item in items:
        key = item_key(item)
        if key in seen:
            continue
        if conn.execute(_DELIVERED, (channel, key[0], key[1])).fetchone() is not None:
            continue
        seen.add(key)
        pending.append(item)
    return pending


def record_delivery(conn, channel, items):
    conn.executemany(
        "INSERT OR IGNORE INTO deliveries (channel, source, link) VALUES (?, ?, ?)",
        [(channel, source, link) for source, link in (item_key(i) for i in items)],
    )


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
