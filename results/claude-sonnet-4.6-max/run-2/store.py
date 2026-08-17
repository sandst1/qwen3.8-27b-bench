"""SQLite persistence.

The `items` table is an archive of everything we have ever seen.  It also
drives deduplication: seen_before() queries it so that run_once() can skip
items that have already been dispatched.

Deduplication key
-----------------
The three feed formats do not share a single stable identity field, so we
combine two signals:

  (a) link  — the story URL.  Stable for wire and blogroll.
              NOT stable for newsroom: UTM campaign tags change every week
              (utm_campaign=w33 → w34), producing a new URL for the same story.

  (b) raw_id — a provider-assigned identifier.
              Stable for newsroom (entry_id).
              NOT stable for the wire (generic) format: the provider regenerates
              the guid whenever an item is edited (typos, added tags, retitles).
              NULL for blogroll (no id at all).

An item is considered "seen" if EITHER condition matches for the same source:
  • its link equals an existing row's link, OR
  • its raw_id is non-null and equals an existing row's raw_id.

Condition (a) catches wire and blogroll duplicates.
Condition (b) catches newsroom items whose URLs have rotated UTM parameters.
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
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def seen_before(conn, item):
    """Return True if *item* has already been recorded in the archive.

    See the module docstring for the rationale behind the two-condition check.
    """
    row = conn.execute(
        "SELECT 1 FROM items"
        " WHERE source = ?"
        "   AND (link = ? OR (raw_id IS NOT NULL AND ? IS NOT NULL AND raw_id = ?))"
        " LIMIT 1",
        (item["source"], item["link"], item.get("raw_id"), item.get("raw_id")),
    ).fetchone()
    return row is not None


def record_items(conn, source, items):
    """Append *items* to the archive.

    Callers are responsible for pre-filtering with seen_before(); this
    function inserts unconditionally and does not deduplicate on its own.
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


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
