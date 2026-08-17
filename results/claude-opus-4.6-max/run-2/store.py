"""SQLite persistence.

Tables
------
items   Write-only archive of every item we have ever fetched.  Useful for
        auditing ("did this ever come through?").  Not used for deduplication.

sent    Tracks which items have been delivered to each channel, keyed on a
        *fingerprint* of the item's link URL (see ``fingerprint()``).  This is
        what prevents the same story from being sent to a channel twice across
        cron runs.

Why fingerprint on link URL (query string stripped)?
----------------------------------------------------
Each feed format has a different reliability profile for identifiers:

*   **newsroom** — stable ``entry_id``, but the feed URL carries UTM params
    that rotate between polls (``utm_campaign=w33`` -> ``w34``).
*   **blogroll** — no identifier at all (``raw_id is None``); the permalink
    is stable.
*   **generic / wire** — has a ``guid``, but the provider regenerates it on
    every edit.  The link itself is stable.

The link (minus query string and fragment) is the only value that is both
present and stable across all three formats.
"""

import sqlite3
from urllib.parse import urlparse, urlunparse

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

CREATE TABLE IF NOT EXISTS sent (
    fingerprint TEXT NOT NULL,
    channel     TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (fingerprint, channel)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def fingerprint(link):
    """Normalise a link URL into a dedup key.

    Strips query string and fragment so that tracking parameters (e.g. UTM
    tags that rotate every poll cycle) do not defeat deduplication.
    """
    p = urlparse(link)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def unsent_items(conn, channel, items):
    """Return the subset of *items* not yet sent to *channel*."""
    if not items:
        return []
    fps = [fingerprint(i["link"]) for i in items]
    unique_fps = list(set(fps))
    placeholders = ",".join("?" * len(unique_fps))
    already = {
        row["fingerprint"]
        for row in conn.execute(
            f"SELECT fingerprint FROM sent"
            f" WHERE channel = ? AND fingerprint IN ({placeholders})",
            [channel] + unique_fps,
        )
    }
    return [i for i, fp in zip(items, fps) if fp not in already]


def mark_sent(conn, channel, items):
    """Record that *items* have been delivered to *channel*."""
    conn.executemany(
        "INSERT OR IGNORE INTO sent (fingerprint, channel) VALUES (?, ?)",
        [(fingerprint(i["link"]), channel) for i in items],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


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
