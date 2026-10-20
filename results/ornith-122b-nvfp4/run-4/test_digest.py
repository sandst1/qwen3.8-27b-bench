"""Behavioural tests for digest.py, driven by the fixtures/.

Run with:  python3 -m unittest -v
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

import digest
import feeds
import store

REPO = os.path.dirname(os.path.abspath(__file__))


def cfg_for(snapshot):
    base = f"file://{REPO}/fixtures/{snapshot}"
    return {
        "feeds": [
            {"name": "newsroom", "format": "newsroom", "url": f"{base}/newsroom.json"},
            {"name": "blogroll", "format": "blogroll", "url": f"{base}/blogroll.json"},
            {"name": "wire", "format": "generic", "url": f"{base}/wire.json"},
        ],
        "channels": [
            {"name": "ops", "type": "stdout", "title": "Ops digest", "keywords": ["port", "levy", "fee"]},
            {"name": "energy", "type": "stdout", "title": "Energy digest", "keywords": ["tender", "grid", "offshore"]},
            {"name": "everything", "type": "stdout", "title": "Firehose", "keywords": []},
        ],
    }


def run(cfg, db_path, dry_run=False):
    """Run one tick, return (sent_count, captured_stdout)."""
    conn = store.connect(db_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        sent = digest.run_once(cfg, conn, dry_run=dry_run)
    conn.close()
    return sent, buf.getvalue()


class DedupKeyTest(unittest.TestCase):
    def test_query_string_is_ignored(self):
        a = {"link": "https://newsroom.example/2026/08/port-fees?utm_campaign=w33"}
        b = {"link": "https://newsroom.example/2026/08/port-fees?utm_campaign=w34"}
        self.assertEqual(store.dedup_key(a), store.dedup_key(b))

    def test_raw_id_is_irrelevant(self):
        # Same wire article, guid regenerated on edit.
        a = {"link": "https://wire.example/i/0031", "raw_id": "wire-2026-08-14-0031"}
        b = {"link": "https://wire.example/i/0031", "raw_id": "wire-2026-08-14-0031-r2"}
        self.assertEqual(store.dedup_key(a), store.dedup_key(b))

    def test_different_paths_differ(self):
        self.assertNotEqual(
            store.dedup_key({"link": "https://x.example/a"}),
            store.dedup_key({"link": "https://x.example/b"}),
        )


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "digest.sqlite3")

    def test_first_tick_sends(self):
        sent, out = run(cfg_for("snapshot-a"), self.db)
        self.assertEqual(sent, 12)  # ops 3 + energy 3 + firehose 6
        self.assertIn("=== ops ===", out)
        self.assertIn("=== energy ===", out)
        self.assertIn("=== everything ===", out)

    def test_second_tick_sends_nothing(self):
        run(cfg_for("snapshot-a"), self.db)
        sent, out = run(cfg_for("snapshot-a"), self.db)
        self.assertEqual(sent, 0)
        self.assertNotIn("=== ops ===", out)
        self.assertNotIn("=== energy ===", out)

    def test_only_genuinely_new_item_surfaces(self):
        # snapshot-a first, then snapshot-b against the same archive.
        run(cfg_for("snapshot-a"), self.db)
        sent, out = run(cfg_for("snapshot-b"), self.db)
        # snapshot-b adds exactly one new article (the union response); every
        # other item is a re-fetch or an edit of something already delivered.
        self.assertEqual(sent, 2)  # ops 1 + firehose 1
        self.assertIn("Union responds to port fee inquiry", out)
        # The already-delivered port-fees article must not re-appear.
        self.assertNotIn("Regulator opens inquiry into port fees", out)

    def test_edited_item_is_not_resent(self):
        # blogroll "port fee arithmetic" is retitled and wire 0031 gains an
        # edit in snapshot-b; both keep their link, so neither should resend.
        run(cfg_for("snapshot-a"), self.db)
        _, out = run(cfg_for("snapshot-b"), self.db)
        self.assertNotIn("port-fee-arithmetic", out)  # blogroll link
        self.assertNotIn("wire.example/i/0031", out)  # wire link

    def test_dry_run_does_not_burn_the_backlog(self):
        # A dry run sends nothing and archives nothing.
        sent_dry, out_dry = run(cfg_for("snapshot-a"), self.db, dry_run=True)
        self.assertEqual(sent_dry, 0)
        self.assertIn("would send to ops", out_dry)  # it did compute the digest
        # A real run on the same archive must still deliver everything.
        sent_real, _ = run(cfg_for("snapshot-a"), self.db)
        self.assertEqual(sent_real, 12)

    def test_archive_records_only_delivered_items(self):
        run(cfg_for("snapshot-a"), self.db)
        conn = store.connect(self.db)
        # Every item was delivered (firehose matches all), so all six are kept,
        # exactly once each.
        self.assertEqual(store.count_items(conn), 6)
        keys = [r["dedup_key"] for r in conn.execute("SELECT dedup_key FROM items")]
        self.assertEqual(len(keys), len(set(keys)))  # no duplicate rows
        conn.close()


class MigrationTest(unittest.TestCase):
    def test_older_db_without_dedup_key_is_backfilled(self):
        # Simulate a pre-dedup database: old schema (no dedup_key), already
        # holding what snapshot-a delivered. Then connect through the migrator.
        import sqlite3

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "legacy.sqlite3")
        con = sqlite3.connect(path)
        con.executescript(
            """
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, raw_id TEXT, title TEXT NOT NULL,
                link TEXT NOT NULL, summary TEXT, published TEXT,
                first_seen TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        all_items = []
        for feed in cfg_for("snapshot-a")["feeds"]:
            all_items.extend(feeds.fetch(feed))
        con.executemany(
            "INSERT INTO items (source, raw_id, title, link, summary, published)"
            " VALUES (:source, :raw_id, :title, :link, :summary, :published)",
            all_items,
        )
        con.commit()
        con.close()

        conn = store.connect(path)  # must not raise, must backfill
        key = conn.execute(
            "SELECT dedup_key FROM items WHERE source = 'newsroom' AND link LIKE '%port-fees%'"
        ).fetchone()["dedup_key"]
        # The rotated utm_campaign must have been stripped.
        self.assertEqual(key, "https://newsroom.example/2026/08/port-fees")
        conn.close()

        # snapshot-b against the migrated archive: its port-fees article (w34)
        # normalises to the same key, so only the genuinely new union article
        # is delivered.
        sent, out = run(cfg_for("snapshot-b"), path)
        self.assertEqual(sent, 2)  # ops 1 + firehose 1
        self.assertIn("Union responds to port fee inquiry", out)
        self.assertNotIn("Regulator opens inquiry into port fees", out)


if __name__ == "__main__":
    unittest.main()
