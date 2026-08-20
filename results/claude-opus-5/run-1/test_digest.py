"""Tests for the deduplication behaviour.

Run with: python3 -m unittest discover -v

The interesting cases all come from the two fixture snapshots. snapshot-b is
snapshot-a a week later, and each of the three feeds mutates a *different*
field that a naive implementation would mistake for a new item:

  * newsroom rotates utm_campaign on every url   (breaks "identify by link")
  * blogroll edits a title and an excerpt        (breaks "identify by title")
  * wire regenerates a guid on edit              (breaks "identify by guid")

Only one item is genuinely new in snapshot-b (newsroom entry_id 84130).
If someone "simplifies" the identity rules in feeds.py, these tests fail.
"""

import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path

import digest
import feeds
import store

FIXTURES = Path(__file__).parent / "fixtures"

FEEDS = [
    {"name": "newsroom", "format": "newsroom", "url": "newsroom.json"},
    {"name": "blogroll", "format": "blogroll", "url": "blogroll.json"},
    {"name": "wire", "format": "generic", "url": "wire.json"},
]

CHANNELS = [
    {"name": "ops", "type": "stdout", "title": "Ops", "keywords": ["port", "levy", "fee"]},
    {"name": "energy", "type": "stdout", "title": "Energy", "keywords": ["tender", "grid"]},
    {"name": "everything", "type": "stdout", "title": "All", "keywords": []},
]


def run_once(cfg, db, **kw):
    """digest.run_once with the stdout channels muted, so a failing test is
    readable instead of buried under rendered digest bodies."""
    with contextlib.redirect_stdout(io.StringIO()):
        return digest.run_once(cfg, db, **kw)


def config_for(snapshot):
    return {
        "feeds": [
            {**f, "url": f"file://{FIXTURES / snapshot / f['url']}"} for f in FEEDS
        ],
        "channels": copy.deepcopy(CHANNELS),
    }


def keys_for(snapshot):
    keys = {}
    for feed_cfg in config_for(snapshot)["feeds"]:
        for item in feeds.fetch(feed_cfg):
            keys[item["key"]] = item
    return keys


class ItemIdentityTests(unittest.TestCase):
    def test_rotating_utm_parameter_is_not_a_new_item(self):
        """newsroom re-stamps every url weekly; the items are unchanged."""
        a, b = keys_for("snapshot-a"), keys_for("snapshot-b")
        moved = [k for k in a if k.startswith("newsroom:")]
        self.assertTrue(moved, "expected newsroom items in the fixture")
        for key in moved:
            self.assertIn(key, b, "newsroom item changed identity on utm rotation")

    def test_edited_title_and_excerpt_is_not_a_new_item(self):
        """blogroll retitled a post to '... (updated)' and rewrote the excerpt."""
        a, b = keys_for("snapshot-a"), keys_for("snapshot-b")
        key = "blogroll:link:https://blog.example/port-fee-arithmetic"
        self.assertIn(key, a)
        self.assertIn(key, b, "blogroll item changed identity when retitled")
        self.assertNotEqual(
            a[key]["title"], b[key]["title"], "fixture no longer exercises the edit"
        )

    def test_regenerated_guid_is_not_a_new_item(self):
        """wire bumped guid 'wire-...-0031' to '...-0031-r2' on an edit."""
        a, b = keys_for("snapshot-a"), keys_for("snapshot-b")
        key = "wire:link:https://wire.example/i/0031"
        self.assertIn(key, a)
        self.assertIn(key, b, "wire item changed identity when its guid was bumped")
        self.assertNotEqual(a[key]["raw_id"], b[key]["raw_id"],
                            "fixture no longer exercises the guid bump")

    def test_only_one_item_is_genuinely_new_in_snapshot_b(self):
        new = set(keys_for("snapshot-b")) - set(keys_for("snapshot-a"))
        self.assertEqual(new, {"newsroom:id:84130"})

    def test_keys_are_namespaced_by_source(self):
        """Two providers carrying the same story stay distinct items."""
        k1 = feeds.item_key("a", None, "https://x.example/1", "T", trust_raw_id=False)
        k2 = feeds.item_key("b", None, "https://x.example/1", "T", trust_raw_id=False)
        self.assertNotEqual(k1, k2)

    def test_normalise_link_strips_tracking_but_keeps_real_params(self):
        self.assertEqual(
            feeds.normalise_link("https://e.example/p?id=7&utm_source=feed#top"),
            "https://e.example/p?id=7",
        )


class RunLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = store.connect(str(Path(self.tmp.name) / "d.sqlite3"))
        self.addCleanup(self.db.close)

    def run_snapshot(self, snapshot, **kw):
        sent, failed = run_once(config_for(snapshot), self.db, **kw)
        return sent

    def test_second_run_on_unchanged_feeds_sends_nothing(self):
        """The reported bug: cron every 15 minutes resent everything."""
        self.assertGreater(self.run_snapshot("snapshot-a"), 0)
        self.assertEqual(self.run_snapshot("snapshot-a"), 0)
        self.assertEqual(self.run_snapshot("snapshot-a"), 0)

    def test_only_the_new_item_is_sent_after_feeds_update(self):
        self.run_snapshot("snapshot-a")
        # 84130 matches 'ops' (port/fee) and 'everything', not 'energy'.
        self.assertEqual(self.run_snapshot("snapshot-b"), 2)
        self.assertEqual(self.run_snapshot("snapshot-b"), 0)

    def test_each_channel_is_tracked_independently(self):
        """An item delivered to one channel must still reach the others."""
        cfg = config_for("snapshot-a")
        first, rest = cfg["channels"][:1], cfg["channels"]
        run_once({**cfg, "channels": first}, self.db)
        # 'ops' is satisfied; the other channels must still receive their items.
        sent, _ = run_once({**cfg, "channels": rest}, self.db)
        self.assertGreater(sent, 0)
        delivered = {
            r[0] for r in self.db.execute("SELECT DISTINCT channel FROM deliveries")
        }
        self.assertEqual(delivered, {"ops", "energy", "everything"})

    def test_dry_run_records_nothing(self):
        self.run_snapshot("snapshot-a", dry_run=True)
        self.assertEqual(store.count_deliveries(self.db), 0)
        self.assertGreater(self.run_snapshot("snapshot-a"), 0)

    def test_seed_suppresses_the_backlog_without_sending(self):
        self.run_snapshot("snapshot-a", seed=True)
        self.assertGreater(store.count_deliveries(self.db), 0)
        self.assertEqual(self.run_snapshot("snapshot-a"), 0)
        # A genuinely new item still comes through afterwards.
        self.assertEqual(self.run_snapshot("snapshot-b"), 2)

    def test_failed_delivery_is_retried_and_does_not_block_other_channels(self):
        """A down webhook must not mark items delivered, nor stop later channels."""
        cfg = config_for("snapshot-a")
        cfg["channels"][0] = {"name": "ops", "type": "nonsense-type",
                              "title": "Ops", "keywords": ["port"]}
        sent, failed = run_once(cfg, self.db)
        self.assertTrue(failed)
        self.assertGreater(sent, 0, "later channels should still have been sent")
        self.assertNotIn(
            "ops",
            {r[0] for r in self.db.execute("SELECT DISTINCT channel FROM deliveries")},
            "a failed send must not be recorded as delivered",
        )
        # Once the channel is fixed, the missed items are delivered.
        sent, failed = run_once(config_for("snapshot-a"), self.db)
        self.assertFalse(failed)
        self.assertGreater(sent, 0)


class ArchiveTests(unittest.TestCase):
    def test_repeated_runs_do_not_duplicate_archive_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = store.connect(str(Path(tmp) / "d.sqlite3"))
            run_once(config_for("snapshot-a"), db)
            after_one = store.count_items(db)
            run_once(config_for("snapshot-a"), db)
            self.assertEqual(store.count_items(db), after_one)
            run_once(config_for("snapshot-b"), db)
            self.assertEqual(store.count_items(db), after_one + 1)
            db.close()

    def test_migration_collapses_a_pre_dedup_database(self):
        """The deployed box's DB has no item_key and duplicate rows."""
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "legacy.sqlite3")
            old = sqlite3.connect(path)
            old.executescript(
                """
                CREATE TABLE items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL, raw_id TEXT, title TEXT NOT NULL,
                    link TEXT NOT NULL, summary TEXT, published TEXT,
                    first_seen TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO items (source, raw_id, title, link, first_seen)
                VALUES ('wire','g1','T','https://w.example/1','2026-01-01'),
                       ('wire','g2','T','https://w.example/1','2026-01-02'),
                       ('wire','g3','T','https://w.example/1','2026-01-03');
                """
            )
            old.commit()
            old.close()

            db = store.connect(path)
            self.assertEqual(store.count_items(db), 1, "duplicates not collapsed")
            row = db.execute("SELECT first_seen FROM items").fetchone()
            self.assertEqual(row["first_seen"], "2026-01-01",
                             "migration should keep the earliest sighting")
            db.close()


if __name__ == "__main__":
    unittest.main()
