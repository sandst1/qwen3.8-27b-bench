"""SQLite persistence.

Two tables live here:

* `items` is an append-only archive of everything we have ever seen, mostly so
  that we can answer "did this ever come through?" when someone asks.
* `delivered` is the dedup ledger. It records, per channel, which items have
  already been sent so the cron run doesn't resend them on the next tick. This
  is the table that actually keeps people from getting the same digest over and
  over; `run_once` in digest.py consults it before every send.

An item's identity for the ledger is (source, link) with any query string
dropped. The link is the only field that stays stable across runs for all
three feed formats: `raw_id` is sometimes absent (blogroll) and sometimes
rewritten when an item is edited (generic), and feed providers tack tracking
params (UTM tags, campaign ids) onto the link that change per fetch while the
article underneath does not.
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
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

CREATE TABLE IF NOT EXISTS delivered (
    channel   TEXT NOT NULL,
    source    TEXT NOT NULL,
    link      TEXT NOT NULL,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, link)
);
"""


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


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


def _dedup_key(item):
    """Identity of an item for the dedup ledger: source + link without query."""
    parts = urlsplit(item.get("link") or "")
    canonical = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return (item.get("source", ""), canonical)


def unseen(conn, channel, items):
    """Return the subset of `items` not yet delivered to `channel`."""
    if not items:
        return []
    keys = [_dedup_key(i) for i in items]
    placeholders = ",".join("(" + ",".join("?" * 2) + ")" for _ in keys)
    params = [channel]
    params.extend(key for k in keys for key in k)
    seen = conn.execute(
        "SELECT source, link FROM delivered "
        "WHERE channel = ? AND (source, link) IN (" + placeholders + ")",
        params,
    ).fetchall()
    already = {(r["source"], r["link"]) for r in seen}
    return [i for i in items if _dedup_key(i) not in already]


def record_delivered(conn, channel, items):
    """Mark `items` as delivered to `channel` so future runs skip them."""
    if not items:
        return
    conn.executemany(
        "INSERT INTO delivered (channel, source, link, sent_at) "
        "VALUES (?, ?, ?, datetime('now')) "
        "ON CONFLICT(channel, source, link) DO NOTHING",
        [(channel, *_dedup_key(i)) for i in items],
    )
    conn.commit()
