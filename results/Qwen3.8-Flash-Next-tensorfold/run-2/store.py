"""SQLite persistence: the delivered ledger.

The `delivered` table records what each channel has already received, and
is what stops the 15-minute cron loop from re-sending the same stories.
Feeds serve a rolling window of recent items, so without a ledger every
tick re-sends most of what the previous tick sent.

Two decisions here are load-bearing — do not "simplify" them away:

* Identity is the item's URL stripped to origin+path (see item_key).
  Neither the feed's item id nor the full URL survives as an identifier:
  the blogroll carries no id at all, the wire feed regenerates its guid
  whenever a story is edited, and the newsroom appends rotating
  utm_source/utm_campaign tags to the same story (both fixture
  snapshots demonstrate this). Origin+path is stable in all three
  formats. Known trade-off: a provider that identifies distinct stories
  via query string (…/article?id=N) would be collapsed to one item by
  this rule; none of our three providers does that today.
* The ledger is keyed per channel, not globally. "Already seen" is a
  fact about an audience: a story already delivered to #ops is still
  news to #firehose.

A delivery is written to the ledger only after it succeeded. If a send
fails, the exception aborts the run (see channels.py), the ledger stays
empty for those items, and the next cron tick retries them.
"""

import sqlite3
from urllib.parse import urlsplit

SCHEMA = """
CREATE TABLE IF NOT EXISTS delivered (
    channel  TEXT NOT NULL,
    item_key TEXT NOT NULL,
    sent_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, item_key)
) WITHOUT ROWID;
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def item_key(item):
    """The ledger key for an item: its URL minus query and fragment.

    Stripping the query string is what lets a story served twice with
    different tracking tags (or with a regenerated guid over the same
    link) be recognised as the same story. If a link is not a usable
    URL, fall back to the raw id so the item still gets *some* key.
    """
    link = (item.get("link") or "").strip()
    parts = urlsplit(link)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"
    return link or f"raw:{item.get('raw_id') or ''}"


def delivered_keys(conn, channel):
    """Keys of items `channel` has already received on an earlier run."""
    cur = conn.execute("SELECT item_key FROM delivered WHERE channel = ?", (channel,))
    return {row["item_key"] for row in cur}


def mark_delivered(conn, channel, items):
    """Record that `channel` received `items`, and commit immediately.

    Committing per channel is deliberate: if a later channel's send fails
    and kills the process, the channels that already got their digest must
    not be sent it again on the next cron tick.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO delivered (channel, item_key) VALUES (?, ?)",
        [(channel, item_key(item)) for item in items],
    )
    conn.commit()
