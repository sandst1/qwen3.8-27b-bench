"""SQLite persistence.

Two tables with two jobs:

* `items` is an append-only archive of everything we have ever seen, so we can
  answer "did this ever come through?" later. It is deliberately *not*
  de-duplicated — the same article lands here once per fetch — and delivery
  logic does not read it.
* `sent` records which items we have already delivered to which channel. This
  is what stops a channel getting the same item on every 15-minute tick.
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

-- (dedup_key, channel) -> we have already delivered this item here.
-- PRIMARY KEY makes re-delivery impossible and is safe to re-run: a fresh DB
-- simply starts with an empty table, so a newly added channel sees the current
-- batch once and then only new items from then on.
CREATE TABLE IF NOT EXISTS sent (
    dedup_key TEXT NOT NULL,
    channel   TEXT NOT NULL,
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (dedup_key, channel)
);
"""


# Query params that churn between fetches without changing what the article
# is. newsroom re-appends these with a fresh campaign on every pull, so
# comparing the raw URL would make an unchanged article look new each time.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "gbraid", "wbraid", "fbclid",
}


def _canonical_link(item):
    """The item's URL with tracking params stripped, so those don't fool dedup."""
    parts = urlsplit(item["link"])
    if not parts.query:
        return item["link"]
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in _TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def dedup_key(item):
    """A stable identity for an item, used to decide whether it was delivered.

    We key on (source, canonical-link) rather than the provider's own id,
    because the three providers cannot agree on one and no single field is
    stable across all of them:

      * newsroom has a stable `entry_id`, but rewrites its URL's tracking
        params on every pull — so keying on the raw URL would resend it.
      * wire keeps a stable URL but regenerates its `guid` on every edit — so
        keying on the id would resend an edited article.
      * blogroll has no id at all — the URL is all we have.

    The article's canonical URL (tracking params dropped) is the one reference
    that stays put in all three cases, so that is what we key on. `source` is
    prepended so two feeds that quote the same URL are treated as distinct.

    If a provider ever changes an item's *path* when it edits it, that item
    will look new and be sent once more — the acceptable price for a key that
    does not depend on any one provider's id scheme.
    """
    return f"{item['source']}\x1f{_canonical_link(item)}"


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


def seen_keys(conn, channel):
    """dedup_keys already delivered to `channel` on a previous run."""
    rows = conn.execute("SELECT dedup_key FROM sent WHERE channel = ?", (channel,))
    return {r["dedup_key"] for r in rows}


def mark_sent(conn, channel, keys):
    """Record that `keys` were delivered to `channel` on this run.

    Call this *after* the channel has actually accepted the digest, never
    before: if delivery fails the exception propagates to cron for a retry, and
    these keys stay unmarked so the same items go out again next tick.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO sent (dedup_key, channel) VALUES (?, ?)",
        [(k, channel) for k in keys],
    )
    conn.commit()
