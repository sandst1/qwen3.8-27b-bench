"""SQLite persistence.

Two tables:

* `items` — an archive of everything we have ever seen. Idempotent: the same
  item (same identity, see below) is stored once, so the table stays a clean
  "did this ever come through?" record instead of growing a duplicate row on
  every 15-minute cron tick.

* `sent`  — which (channel, item) pairs have already been delivered. This is
  what stops people getting the same items over and over: an item is only sent
  to a channel the first time it matches that channel's filter.

Identity / the dedup key
------------------------
Each feed format is only half-reliable about identifiers:

* newsroom  -> a stable `entry_id`, but the URL carries rotating `utm_*` params
* blogroll  -> no identifier at all, just a stable permalink
* wire      -> a `guid` that is regenerated whenever the item is edited

So neither `raw_id` nor the raw `link` is a dependable key on its own: `raw_id`
is missing or unstable, and the newsroom link changes every week purely because
of its tracking query string. The one thing that is stable for all three is the
link *with the tracking query params stripped*. We therefore identify an item
by **(source, canonical link)** and store those two columns in `sent`.

Trade-off: if a provider ever *moves* an article to a genuinely new URL, we
treat it as new and resend it. That is the right behaviour here (a new URL is
all a reader has to go on), and the fixtures confirm links are otherwise
stable.

Delivery is at-least-once: we record a send only *after* the channel accepts
it, so a crash between the two resends the item once rather than dropping it.
For a digest, a rare duplicate is far better than a missed item.
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
    norm_link   TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, norm_link)
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

CREATE TABLE IF NOT EXISTS sent (
    channel    TEXT NOT NULL,
    source     TEXT NOT NULL,
    norm_link  TEXT NOT NULL,
    sent_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, norm_link)
);
"""

# Query params that exist only for click-tracking. They change from run to run
# without changing the article, so they must not be part of the identity.
# Anything starting with "utm_" is stripped regardless of this list.
_TRACKING_PARAMS = {
    "fbclid", "gclid", "mc_cid", "mc_eid",
    "ref", "ref_src", "ref_url",
}


def normalize_link(link):
    """Strip tracking query params so the same article yields the same key."""
    parts = urllib.parse.urlsplit(link)
    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    query = urllib.parse.urlencode(kept)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def identity(item):
    """(source, canonical link) — the stable identity we dedup on."""
    return item["source"], normalize_link(item["link"])


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_items(conn, source, items):
    """Archive items; a repeat of an already-seen item is ignored."""
    conn.executemany(
        "INSERT OR IGNORE INTO items"
        " (source, raw_id, title, link, norm_link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                source, i["raw_id"], i["title"], i["link"],
                normalize_link(i["link"]), i.get("summary"), i.get("published"),
            )
            for i in items
        ],
    )
    conn.commit()


def sent_keys(conn, channel):
    """The set of (source, canonical link) already delivered to this channel."""
    return {
        (row["source"], row["norm_link"])
        for row in conn.execute(
            "SELECT source, norm_link FROM sent WHERE channel = ?", (channel,)
        )
    }


def mark_sent(conn, channel, items):
    """Record that these items have been delivered to this channel."""
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, source, norm_link) VALUES (?, ?, ?)",
        [(channel, *identity(i)) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
