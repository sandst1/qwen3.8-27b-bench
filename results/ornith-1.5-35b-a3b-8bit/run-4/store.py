"""SQLite persistence.

Two tables, two concerns:

`items`
    Append-only archive of everything we have ever pulled from a feed. Kept so
    we can answer "did this ever come through?" when someone asks. Nothing
    here drives delivery decisions.

`sent_items`
    What we have already delivered, keyed by (source, link). This is the only
    thing that stops the same item being pushed to a channel twice.

Identity. An item is identified by its (source, link) pair, where `link` is the
item's URL with the query string dropped. We deliberately do NOT use the
providers' own ids as identity:

    * newsroom exposes a stable entry_id, but its link carries a changing
      `utm_campaign` param, so the raw link is not stable.
    * blogroll has no id at all.
    * the generic wire regenerates its `guid` every time an item is edited.

The URL path is the one field that stays put across all three once tracking
params are stripped, so that is what we key on. Two items from the same source
that resolve to the same normalized URL are treated as the same item.
"""

import sqlite3
from urllib.parse import urlsplit

ITEMS_SCHEMA = """
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
"""

SENT_ITEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_items (
    source   TEXT NOT NULL,
    link     TEXT NOT NULL,
    sent_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, link)
);
"""

SCHEMA = ITEMS_SCHEMA + "\n" + SENT_ITEMS_SCHEMA


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


def link_key(link):
    """Canonical identity for a URL: scheme + host + path, tracking stripped."""
    parts = urlsplit(link or "")
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def sent_keys(conn):
    """Set of (source, link) pairs we have already delivered."""
    return {
        (r["source"], r["link"])
        for r in conn.execute("SELECT source, link FROM sent_items").fetchall()
    }


def pending_items(conn, items):
    """Items from `items` that have not been delivered yet, in input order."""
    seen = sent_keys(conn)
    return [i for i in items if (i["source"], link_key(i["link"])) not in seen]


def mark_sent(conn, items):
    conn.executemany(
        "INSERT OR REPLACE INTO sent_items (source, link, sent_at)"
        " VALUES (?, ?, datetime('now'))",
        [(i["source"], link_key(i["link"])) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
