"""SQLite persistence.

The `items` table is an archive of everything we have actually *delivered* to a
channel. It serves two jobs:

  1. Deduplication — `run_once` asks `seen_keys` whether it has archived an
     item before, and skips anything it has. Without this a cron tick running
     every 15 minutes re-delivers the entire backlog on every tick, which is
     the bug this table exists to stop.
  2. History — answering "did this ever come through?" when someone asks.

We archive only what was delivered (not merely fetched): an item that matched
no channel this tick is left un-archived, so if a matching channel is added
later the item can still be delivered. The trade-off is that dedup means
"don't re-deliver what we already sent", not "don't re-deliver what we ever
looked at".
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
    dedup_key   TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    return conn


def _migrate(conn):
    """Bring an older `items` table up to date.

    Databases created before deduplication have no `dedup_key` column. Add it
    and backfill it from `link` so the very first post-deploy run does not
    re-deliver everything already in the archive. Runs once, then no-ops.

    The index is created here rather than in SCHEMA because on a legacy table
    `CREATE INDEX ... ON items(dedup_key)` would run before the column exists
    and fail; creating it after the add/backfill is always safe.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "dedup_key" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN dedup_key TEXT")
        # Mirror dedup_key() in Python: drop the query string, keep the rest.
        conn.execute(
            "UPDATE items SET dedup_key ="
            " CASE WHEN link LIKE '%?%' THEN substr(link, 1, instr(link, '?') - 1)"
            " ELSE link END"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_dedup_key ON items(dedup_key)")
    conn.commit()


def dedup_key(item):
    """The identity we dedup on: the item's link with any query string stripped.

    Why the link path, and not `raw_id`? The three feeds disagree on how to
    identify an item, and no single `raw_id` is reliable across all of them:

      * newsroom — `raw_id` (entry_id) is stable, but its link carries a
        rotating `utm_campaign` (w33 one tick, w34 the next) for the *same*
        article. Keying on the raw link would re-send it every tick.
      * wire — its link is stable, but `raw_id` (the guid) is regenerated
        whenever the story is edited (an `-r2` suffix, etc.). Keying on
        `raw_id` would re-send every edited story.
      * blogroll — has no `raw_id` at all.

    The link *path* is the one field that is stable for all three across
    re-fetches and edits, so that is the identity. Stripping the query string
    is what makes newsroom's rotated utm params collapse to the same key.

    If a feed ever ships an item with no link, this returns "" and those items
    are effectively never deduped (each tick treats them as new) — acceptable,
    since every normaliser in feeds.py always sets a link.
    """
    return (item.get("link") or "").split("?", 1)[0]


def seen_keys(conn, keys):
    """Return the subset of `keys` already present in the archive."""
    keys = list(keys)
    if not keys:
        return set()
    placeholders = ", ".join(["?"] * len(keys))
    rows = conn.execute(
        f"SELECT dedup_key FROM items WHERE dedup_key IN ({placeholders})",
        keys,
    )
    return {row["dedup_key"] for row in rows}


def record_items(conn, source, items):
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published, dedup_key)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"), dedup_key(i))
            for i in items
        ],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
