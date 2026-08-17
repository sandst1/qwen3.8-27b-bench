"""SQLite persistence.

Two tables with two distinct jobs:

``items``
    A de-duplicated archive of every article we have ever *seen*, keyed by
    (source, normalised link). Written the first time we see an article.
    Exists to answer "did this ever come through?".

``sent``
    The per-channel delivery log, and the thing that actually stops us from
    notifying people about the same article twice. A row (channel, source,
    normalised link) means "this channel already received this article". It is
    written only *after* a delivery succeeds, which is what makes delivery
    at-least-once: if a channel is down the article is not logged, so the next
    cron tick retries it. A healthy channel is never re-sent an article just
    because a sibling channel failed.

Why the normalised link as the identity, and not the feed's own id?

The three feeds do not agree on a stable identifier:

* ``newsroom`` has a stable ``entry_id``, but it rewrites the query string
  (``utm_campaign``) on the same article's URL between polls, so the raw URL
  is not stable either;
* ``blogroll`` has no id at all -- only the permalink is stable (the title can
  change on an edit);
* ``generic`` has a ``guid`` the provider regenerates whenever an item is
  edited, so the id of the same article changes.

The one thing that is stable in all three is the article's URL with the query
string and fragment stripped (i.e. its path). ``normalise_link`` computes it,
and it is the key for both tables.

Caveat: two genuinely different articles that differ only by query string
(e.g. ``?page=2``) would be treated as the same. That does not happen for the
permalinks these feeds serve; if it ever does, revisit this.

To force a re-send of everything (after a long outage, or to re-notify after a
config change), delete the ``sent`` table; the next run starts fresh.
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

CREATE TABLE IF NOT EXISTS sent (
    channel   TEXT NOT NULL,
    source    TEXT NOT NULL,
    link      TEXT NOT NULL,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, link)
);
"""


def normalise_link(url):
    """Return `url` with its query string and fragment removed.

    This is the stable identity of an article across all three feeds; see the
    module docstring for why the feed-native ids are not usable.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_items(conn, source, items):
    """Archive items we have not already seen.

    The feeds re-serve the same items on every poll, so we compare on the
    normalised link and skip articles that are already in the archive rather
    than re-inserting a copy of them.
    """
    if not items:
        return
    seen = {
        normalise_link(row["link"])
        for row in conn.execute("SELECT link FROM items WHERE source = ?", (source,))
    }
    fresh = [i for i in items if normalise_link(i["link"]) not in seen]
    if not fresh:
        return
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in fresh
        ],
    )
    conn.commit()


def unsent(conn, channel, items):
    """Return the items in `items` that `channel` has not already received.

    This is the de-duplication step: an article is delivered to a given channel
    at most once, so on each poll we drop everything the channel already got.
    """
    if not items:
        return []
    sent_keys = {
        (row["source"], row["link"])
        for row in conn.execute("SELECT source, link FROM sent WHERE channel = ?", (channel,))
    }
    return [i for i in items if (i["source"], normalise_link(i["link"])) not in sent_keys]


def mark_sent(conn, channel, items):
    """Log that `channel` received `items`.

    Called only after a successful delivery, so a failed send is not logged and
    will be retried on the next run (at-least-once delivery).
    """
    if not items:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO sent (channel, source, link) VALUES (?, ?, ?)",
        [(channel, i["source"], normalise_link(i["link"])) for i in items],
    )
    conn.commit()


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
