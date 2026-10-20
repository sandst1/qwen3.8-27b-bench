"""SQLite persistence.

The `items` table preserves feed observations (including edits and repeats).
The separate delivery ledger records successful delivery per channel/article.
An observation is not evidence of delivery, including in pre-ledger databases.
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

CREATE TABLE IF NOT EXISTS deliveries (
    channel     TEXT NOT NULL,
    source      TEXT NOT NULL,
    article_url TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, source, article_url)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_items(conn, source, items):
    """Append observations; the caller owns the transaction."""
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in items
        ],
    )


def article_key(item):
    """Edits and feed GUID churn do not make a new article. See README.md."""
    url = urlsplit(item["link"])
    query = [(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    link = urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path,
                       urlencode(query), ""))
    return item["source"], link


def pending_items(conn, channel, current_items=()):
    """Latest observation of each article not yet delivered to this channel.

    Current items let dry runs preview new observations without writing them.
    Reading the archive also keeps failed deliveries retryable after feed expiry.
    """
    latest = {}
    for row in conn.execute("SELECT * FROM items ORDER BY id"):
        item = dict(row)
        latest[article_key(item)] = item
    for item in current_items:
        latest[article_key(item)] = item
    delivered = {
        (row["source"], row["article_url"])
        for row in conn.execute(
            "SELECT source, article_url FROM deliveries WHERE channel = ?", (channel,)
        )
    }
    return [item for key, item in latest.items() if key not in delivered]


def mark_delivered(conn, channel, items):
    """Call only after send returns successfully, inside the selection transaction."""
    conn.executemany(
        "INSERT INTO deliveries (channel, source, article_url) VALUES (?, ?, ?)",
        [(channel, *article_key(item)) for item in items],
    )


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
