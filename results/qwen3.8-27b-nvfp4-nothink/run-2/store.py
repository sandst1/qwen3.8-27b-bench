"""SQLite persistence.

Two tables:

* `items` — archive of everything we have ever seen, one row per (source,
  raw_id). Used to answer "did this ever come through?" when someone asks.

* `seen` — the dedup state the cron loop relies on. One row per item we have
  already digested, keyed by normalised link (see `normalise_link`).

Why link, not raw_id: the feeds disagree on identifiers. `blogroll` has no
identifier at all, and the `generic` feed's guid changes whenever the provider
edits an item. The canonical link is the only thing that stays stable across
all three, so it is what we dedup on.

Two consequences of that choice, both deliberate:

* Edits to an item (new guid, changed headline, appended comment) are NOT
  re-sent. For a 15-minute cron loop that is the right call — people were
  complaining about repeats, not about missing corrections. If we ever want
  edit notifications, that is a separate feature, not a tweak here.
* If a link moves permanently, the item will be re-sent once. Acceptable for
  this use case.

The first run after an upgrade seeds `seen` from the existing `items`
archive, so nothing that has ever been sent is re-sent.
"""

import sqlite3
from urllib.parse import urlsplit

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
    key         TEXT PRIMARY KEY,
    source      TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def normalise_link(link):
    """Collapse a link to the canonical identity of the item it points at.

    Tracking query params (utm_* and friends) and fragments do not change
    which item a link refers to, and feeds are not consistent about including
    them, so strip them. Trailing slashes also vary feed to feed. The host is
    lowercased (DNS is case-insensitive); the path is kept as-is because
    paths can be case-sensitive.
    """
    parts = urlsplit(link)
    query = tuple(
        kv.split("=", 1)
        for kv in parts.query.split("&")
        if kv and not kv.split("=", 1)[0].startswith("utm_")
    )
    return (
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/"),
        query,
    )


def _encode_key(normalised):
    """Serialise a normalised link for storage.

    The key is only ever written and compared, never decoded back, so a
    simple repr of the tuple (which contains only strings) is enough.
    """
    return repr(normalised)


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # One-time migration for pre-existing databases: the `seen` table is new,
    # so seed it from the archive. INSERT OR IGNORE keeps this idempotent and
    # a no-op for brand-new databases (empty `items`).
    for row in conn.execute("SELECT link, source FROM items"):
        conn.execute(
            "INSERT OR IGNORE INTO seen (key, source) VALUES (?, ?)",
            (_encode_key(normalise_link(row["link"])), row["source"]),
        )
    conn.commit()
    return conn


def record_items(conn, source, items):
    """Archive the raw feed payload.

    `items` keeps one row per (source, raw_id): an edited item gets a new row
    so the archive shows what actually came through, in order.
    """
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (source, i["raw_id"], i["title"], i["link"], i.get("summary"), i.get("published"))
            for i in items
        ],
    )
    conn.commit()


def new_items(conn, source, items):
    """Return the subset of `items` we have not digested yet.

    An item is "new" when its normalised link is not in `seen`. Everything
    returned is recorded in `seen` in the same transaction, so a crash after
    this call cannot re-send the same items.
    """
    fresh = []
    seen_keys = set()
    for item in items:
        key = _encode_key(normalise_link(item["link"]))
        if key in seen_keys:
            continue
        row = conn.execute("SELECT 1 FROM seen WHERE key = ?", (key,)).fetchone()
        if row is None:
            seen_keys.add(key)
            fresh.append((key, item))
    if fresh:
        conn.executemany(
            "INSERT OR IGNORE INTO seen (key, source) VALUES (?, ?)",
            [(key, source) for key, _ in fresh],
        )
        conn.commit()
    return [item for _, item in fresh]


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
