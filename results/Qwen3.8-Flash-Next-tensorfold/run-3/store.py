"""SQLite persistence: an archive, and the ledger that stops repeat sends.

Two tables:

`items`     Archive of everything ever fetched, one row per (feed, URL)
            seen. Nothing in the send path reads it; it exists to answer
            "did this ever come through?" when someone asks.

`delivered` Delivery ledger: one row per (channel, source, key) that a
            digest has already carried for that channel. Feeds re-list
            their back-catalogue on every fetch and cron ticks every 15
            minutes, so without this ledger every digest re-sends the
            whole feed window. An item may match several channels'
            filters and then appear once in each of their digests --
            channels are independent views, which keeps digest content
            from depending on channel order in the config file.

An item is identified by its `key` -- its link normalised via `identity`.
Never by the id the feed provides: see `identity` for why ids and raw
URLs cannot be trusted.
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
    channel  TEXT NOT NULL,
    source   TEXT NOT NULL,
    key      TEXT NOT NULL,
    sent_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, key)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def identity(link):
    """The dedup key for an item: its link with scheme/host lower-cased and
    the query string and fragment stripped.

    Ids cannot key anything (the `generic` provider regenerates its guid
    whenever an item is edited, the `blogroll` provider has none), and the
    raw link is not stable either: the newsroom provider rotates utm_
    tracking tags in the query string between weeks -- fixtures/snapshot-a
    vs snapshot-b show the same story arriving once with utm_campaign=w33
    and once with w34. Keying on the stripped link makes those one item.

    Trade-off to revisit if a new feed ever keys real content in the query
    string (`/view?article=123`-style URLs): this rule would fold such
    distinct articles together. None of the three current providers does.
    """
    parts = urlsplit(link)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


def record_items(conn, source, items):
    """Archive freshly fetched items, skipping ones already on file.

    Feeds re-list the same items for as long as they stay in the feed's
    window; one archive row per item keeps "did this ever come through?"
    answerable without growing the database every tick.
    """
    have = {identity(r["link"]) for r in
            conn.execute("SELECT link FROM items WHERE source = ?", (source,))}
    rows = []
    for i in items:
        key = identity(i["link"])
        if key in have:
            continue
        have.add(key)
        rows.append(
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
        )
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def delivered_keys(conn, channel):
    """(source, key) pairs this channel has already received."""
    return {
        (r["source"], r["key"])
        for r in conn.execute("SELECT source, key FROM delivered WHERE channel = ?", (channel,))
    }


def mark_delivered(conn, channel, items):
    conn.executemany(
        "INSERT OR IGNORE INTO delivered (channel, source, key) VALUES (?, ?, ?)",
        [(channel, i["source"], identity(i["link"])) for i in items],
    )
    conn.commit()


def seed_ledger(conn, channels):
    """One-time upgrade step: treat the old archive as already delivered.

    Before the dedup fix, every cron run re-sent the whole feed window to
    every channel whose filter matched, so anything already sitting in
    `items` had in fact already been sent to everyone many times over.
    Without this seed, the first run *with* the fix would deliver that
    whole stale window one final time -- the exact spam people complained
    about. A database created by the fixed version starts with an empty
    `delivered` table, so nothing is suppressed on the very first run: a
    fresh install legitimately sees the current feed window as new.

    The seeded rows are bookkeeping marks, not real sends (their sent_at is
    the seed timestamp); the ledger is deliberately not used for anything
    beyond dedup, which is why that shortcut is safe.
    """
    if conn.execute("SELECT 1 FROM delivered LIMIT 1").fetchone():
        return
    archived = conn.execute("SELECT source, link FROM items").fetchall()
    if not archived:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO delivered (channel, source, key) VALUES (?, ?, ?)",
        [
            (name, r["source"], identity(r["link"]))
            for name in channels
            for r in archived
        ],
    )
    conn.commit()
