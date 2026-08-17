"""SQLite persistence.

The `items` table records everything we have ever seen AND drives
deduplication.  Call `filter_unseen` before sending to get only the items
that have not been dispatched in a previous run.

Deduplication key: (source, link)
-------------------------------
We considered three candidates:

  raw_id  — unreliable: the blogroll format never sets it (always None),
             and the generic/wire format regenerates it on every edit,
             so the same article can appear with a different raw_id after
             a typo fix.

  title   — not stable; titles get corrected.

  link    — the canonical URL of the item.  All three feed formats always
             provide one, and once published, links do not change.  This
             is the best key we have across all providers.

The UNIQUE constraint on (source, link) lets SQLite enforce this at the
database level so we can use INSERT OR IGNORE for an atomic seen/record
operation.
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
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),

    -- Prevent the same URL from the same source being sent twice.
    -- INSERT OR IGNORE against this constraint is the deduplication mechanism.
    UNIQUE (source, link)
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def filter_unseen(conn, source, items):
    """Return only items whose (source, link) pair is not yet in the DB.

    This is a read-only query; call record_items afterwards to persist them.
    Keeping read and write separate makes dry-run mode accurate: a dry run
    will show what *would* be sent without marking those items as seen.
    """
    if not items:
        return []
    seen_links = {
        row["link"]
        for row in conn.execute(
            "SELECT link FROM items WHERE source = ?", (source,)
        )
    }
    return [i for i in items if i["link"] not in seen_links]


def record_items(conn, source, items):
    """Persist items, silently skipping any that are already recorded.

    INSERT OR IGNORE respects the UNIQUE (source, link) constraint, so
    this is safe to call even if some items were already seen.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in items
        ],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
