"""SQLite persistence.

Two tables with different jobs:

`items` is the archive: an append-only log of every sighting, kept so someone
can answer "did this ever come through?". It is written once per item per
poll and nothing reads it programmatically. It is *not* the dedup mechanism
-- it never was, which is why items were being resent every 15 minutes.

`deliveries` is the dedup ledger, and is the fix. See `identity.py` for how
an item's keys are derived; this module only stores them.

Why the ledger is keyed (channel, ident) and not by ident alone
--------------------------------------------------------------
"Have we seen this item?" is the wrong question. The right one is "have we
sent this item *to this channel*?". A global seen-set breaks two ordinary
cases:

  * two channels match the same item -- with the config as shipped, a story
    about port fees matches both `ops` and the `everything` firehose. Under a
    global set whichever channel is listed first wins and the other never
    hears about it.
  * a channel is added, or its keywords are widened. A global set would have
    already burned every matching item, so the new channel starts deaf.

Per-channel rows cost nothing (a few hundred bytes a day) and make both cases
behave the way an operator expects.

Why `last_seen` exists
----------------------
The ledger would otherwise grow forever. Pruning on "delivered more than N
days ago" would be a trap: an item that a feed keeps publishing for longer
than N days would fall out of the ledger while still on the feed, and get
resent -- reintroducing exactly this bug on a delay, in a form that would
take someone a very long time to reproduce.

So `last_seen` is bumped on every poll in which the item is still present,
and pruning is on `last_seen`. An entry can only be dropped after the item
has been *gone from the feed* for the whole retention window, which makes
pruning safe by construction rather than by choosing a lucky number.
"""

import sqlite3

"""Tables first, then column migrations, then indexes.

The order is load-bearing. `CREATE TABLE IF NOT EXISTS items` is a no-op
against a database that predates the ledger, so that table arrives here
without an `ident` column; an index on `ident` in this same script would
fail with "no such column" and take every existing deployment down on
upgrade. Anything referencing a migrated column belongs in _INDEXES.
"""
_TABLES = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    raw_id      TEXT,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    ident       TEXT
);

-- One row per (channel, identity key). Presence means "already delivered";
-- an item carries several keys and matching any one of them counts.
CREATE TABLE IF NOT EXISTS deliveries (
    channel         TEXT NOT NULL,
    ident           TEXT NOT NULL,
    first_delivered TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, ident)
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_ident ON items(ident);
CREATE INDEX IF NOT EXISTS idx_deliveries_last_seen ON deliveries(last_seen);
"""

# Columns added after the original schema shipped: (table, column, type).
_ADDED_COLUMNS = [
    ("items", "ident", "TEXT"),
]


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_TABLES)
    _migrate(conn)
    conn.executescript(_INDEXES)
    conn.commit()
    return conn


def _migrate(conn):
    """Bring a database created before the dedup ledger up to date.

    Additive only: `deliveries` is created by _TABLES, and `items` just gains
    a nullable column. Existing archive rows keep a NULL ident, which is fine
    -- nothing reads the archive, and rebuilding a live table during a bugfix
    is not a trade worth making.

    An upgraded deployment starts with an empty ledger, so the first poll
    after deploy treats everything currently on the feeds as new. Run once
    with `--seed` to adopt the current feed contents silently instead.
    """
    for table, column, coltype in _ADDED_COLUMNS:
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def record_items(conn, source, items):
    """Append this poll's sightings to the archive."""
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published, ident)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                source,
                i["raw_id"],
                i["title"],
                i["link"],
                i.get("summary"),
                i.get("published"),
                # The primary key, so an archive row can be traced to a
                # ledger row when someone asks why an item did or didn't go.
                i["idents"][0],
            )
            for i in items
        ],
    )
    conn.commit()


def already_delivered(conn, channel, keys):
    """True if any of `keys` has already gone to `channel`."""
    if not keys:
        # keys_for() guarantees this cannot happen; treat the impossible case
        # as "not delivered" so an item is never silently dropped.
        return False
    placeholders = ",".join("?" * len(keys))
    row = conn.execute(
        f"SELECT 1 FROM deliveries WHERE channel = ? AND ident IN ({placeholders}) LIMIT 1",
        (channel, *keys),
    ).fetchone()
    return row is not None


def mark_delivered(conn, channel, keys):
    """Record `keys` as delivered to `channel`, and refresh their last_seen.

    Called on two paths:

      * after a successful send, to stop the item going out again;
      * when an item is recognised as a repeat, so that its keys stay fresh
        for pruning and so any *new* key it has picked up (a rotated guid, a
        moved URL) is linked to the same identity from now on.

    Idempotent, so the two paths cannot conflict.
    """
    if not keys:
        return
    conn.executemany(
        "INSERT INTO deliveries (channel, ident) VALUES (?, ?)"
        " ON CONFLICT(channel, ident) DO UPDATE SET last_seen = datetime('now')",
        [(channel, k) for k in keys],
    )


def prune(conn, retention_days):
    """Forget entries whose item has been absent from the feeds that long.

    Returns the number of rows removed. See the module docstring for why this
    is keyed on last_seen rather than on the delivery date.
    """
    if not retention_days or retention_days <= 0:
        return 0
    cur = conn.execute(
        "DELETE FROM deliveries WHERE last_seen < datetime('now', ?)",
        (f"-{int(retention_days)} days",),
    )
    conn.commit()
    return cur.rowcount


def count_items(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


def count_deliveries(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"]
