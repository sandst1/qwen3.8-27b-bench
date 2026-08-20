"""SQLite persistence.

The `items` table is an archive of everything we have ever seen, mostly so
that we can answer "did this ever come through?" when someone asks. Nothing
reads it at the moment.

The `deliveries` table is what actually prevents re-sending the same item to
the same channel on every cron tick. It is keyed on (channel, dedup_key),
where `dedup_key` is *not* simply `link` or simply `raw_id` — comparing the
two fixture snapshots in fixtures/ shows why neither is safe on its own for
all three providers:

  - "newsroom" items keep a stable `raw_id` (entry_id) but their `link`
    changes between polls (the provider appends a rolling utm_campaign
    tracking param), so keying on link would make the same story look new
    every week and get re-sent.
  - "generic" (wire) items keep a stable `link` but the provider regenerates
    `raw_id` (guid) whenever the item is edited (typo fix, added detail,
    retitle, ...), so keying on raw_id would make an edited item look new
    and get re-sent.
  - "blogroll" items have no raw_id at all, so `link` is the only option.

The rule applied in `feeds.dedup_key()` is therefore: use `raw_id` when the
provider supplies one, otherwise fall back to `link`. This is correct for
newsroom and blogroll. For wire it means a genuine content edit will look
like a "new" item once and be re-sent a single extra time — a much rarer,
more acceptable edge case than the constant duplicate spam this change is
fixing. If a fourth provider shows up with its own quirks, this is the place
to extend the rule.
"""

import sqlite3

import feeds

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
    dedup_key   TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, dedup_key)
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


def undelivered(conn, channel, items):
    """Return the subset of `items` not already recorded as sent to `channel`.

    Items are matched by `feeds.dedup_key(item)`, not by link or raw_id
    directly — see the module docstring for why.
    """
    if not items:
        return []
    keys = list({feeds.dedup_key(i) for i in items})
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT dedup_key FROM deliveries WHERE channel = ? AND dedup_key IN ({placeholders})",
        [channel, *keys],
    )
    already_sent = {r["dedup_key"] for r in rows}
    return [i for i in items if feeds.dedup_key(i) not in already_sent]


def mark_delivered(conn, channel, items):
    conn.executemany(
        "INSERT OR IGNORE INTO deliveries (channel, dedup_key) VALUES (?, ?)",
        [(channel, feeds.dedup_key(i)) for i in items],
    )
    conn.commit()
