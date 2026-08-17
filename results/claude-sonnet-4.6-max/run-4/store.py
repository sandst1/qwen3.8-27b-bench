"""SQLite persistence.

Two tables
----------
items
    Append-only archive of every item ever fetched.  Useful for answering
    "did this story ever come through?" but is never read during dispatch.

seen
    Deduplication ledger.  One row per canonical story identity.  Checked
    on every run so we never re-send an item that was already dispatched.

Canonical identity
------------------
We use  source + link-without-query-string  as the stable key.

Why not raw_id?  Three reasons, one per feed format:

* newsroom  — entry_id is stable, but the link carries a rotating UTM
  campaign tag (?utm_campaign=wNN) that changes every week, so using the
  link naively would re-send the same story on the next campaign rotation.
  We strip the query string to get a stable URL.

* wire/generic  — the provider regenerates the guid whenever an item is
  edited (typos, retitles, added tags).  Using the guid would cause every
  edit to look like a new story.  The link is constant across edits.

* blogroll  — no raw_id at all.  The permalink is the only stable handle.

Dropping the query string from the link gives a single rule that handles
all three feeds without any per-source branching.  The full original link
(including UTMs) is still stored in the items archive and still shown in
rendered digests; we only strip it for the internal dedup key.
"""

import sqlite3
import urllib.parse

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

CREATE TABLE IF NOT EXISTS seen (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    seen_key   TEXT NOT NULL UNIQUE,
    first_seen TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _seen_key(item):
    """Return the canonical dedup key for *item*.

    Format: ``"<source>|<link-without-query-string>"``.
    """
    parsed = urllib.parse.urlparse(item["link"])
    canonical = parsed._replace(query="", fragment="").geturl()
    return f"{item['source']}|{canonical}"


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_items(conn, source, items):
    """Append *items* to the archive (always; duplicates are expected)."""
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in items
        ],
    )
    conn.commit()


def filter_unseen(conn, items):
    """Return the subset of *items* not yet present in the seen ledger."""
    unseen = []
    for item in items:
        row = conn.execute(
            "SELECT 1 FROM seen WHERE seen_key = ?", (_seen_key(item),)
        ).fetchone()
        if row is None:
            unseen.append(item)
    return unseen


def mark_seen(conn, items):
    """Record *items* in the seen ledger so they are skipped on future runs."""
    conn.executemany(
        "INSERT OR IGNORE INTO seen (seen_key) VALUES (?)",
        [(_seen_key(i),) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
