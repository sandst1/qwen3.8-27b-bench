"""SQLite persistence: the per-channel delivery ledger.

`sent` holds one row per (channel, item) pair that was successfully
delivered. Digest selection subtracts it, which is what stops people from
getting the same items on every cron tick: feeds keep serving their rolling
window of recent items, so without this ledger every run re-sends most of
what the previous run sent.

Two deliberate choices:

- The ledger is per-channel, not global. Two channels are two audiences;
  an item one of them received tells us nothing about the other.
- Rows are written only after a successful send. If a webhook is down the
  run dies with DeliveryError (see channels.py) and nothing is recorded,
  so the next tick retries the batch — including items that had already
  gone out to an earlier channel this run.

History is kept rather than deleted so "did X ever reach channel Y?" stays
answerable, and because `item_key` deduplicates per distinct story the
table grows by items-per-day, not items-per-tick (tens of rows a day).

The pre-2026 schema had an `items` archive table that nothing read; new
databases simply do not have it.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS sent (
    channel  TEXT NOT NULL,
    item_key TEXT NOT NULL,
    source   TEXT NOT NULL,
    title    TEXT NOT NULL,
    link     TEXT NOT NULL,
    sent_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, item_key)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def sent_keys(conn, channel):
    """Item keys this channel has already received."""
    rows = conn.execute(
        "SELECT item_key FROM sent WHERE channel = ?", (channel,)
    ).fetchall()
    return {r["item_key"] for r in rows}


def record_sent(conn, channel, keyed_items):
    """Record (item_key, item) pairs as delivered to this channel."""
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, item_key, source, title, link)"
        " VALUES (?, ?, ?, ?, ?)",
        [(channel, key, i["source"], i["title"], i["link"]) for key, i in keyed_items],
    )
    conn.commit()
