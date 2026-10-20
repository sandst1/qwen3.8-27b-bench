"""Offline delivery regressions. Run with python3 -m unittest -v."""

from contextlib import closing
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import channels
import digest
import store


ROOT = Path(__file__).parent


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "digest.sqlite3"
        self.db = store.connect(self.path)
        self.addCleanup(self.db.close)
        self.cfg = digest.load_config(ROOT / "config.example.toml")
        self.snapshot("a")

    def snapshot(self, name):
        for feed in self.cfg["feeds"]:
            feed["url"] = str(ROOT / "fixtures" / f"snapshot-{name}" /
                              f"{feed['name']}.json")

    def test_snapshots_and_restart(self):
        with patch("channels.send") as send:
            self.assertEqual(digest.run_once(self.cfg, self.db), 12)
            self.assertEqual(send.call_count, 3)
            # Delivery receipts must survive a fresh connection/process.
            with closing(store.connect(self.path)) as other:
                self.assertEqual(digest.run_once(self.cfg, other), 0)
            self.snapshot("b")
            send.reset_mock()
            self.assertEqual(digest.run_once(self.cfg, self.db), 2)
            self.assertEqual([call.args[0]["name"] for call in send.call_args_list],
                             ["ops", "everything"])
            for call in send.call_args_list:
                self.assertIn("Union responds", call.args[1])
                self.assertIn("1 item(s)", call.args[1])
            self.assertEqual(digest.run_once(self.cfg, self.db), 0)

    def test_duplicate_rows_in_one_poll(self):
        original = digest.feeds.fetch
        with patch("feeds.fetch", side_effect=lambda cfg: original(cfg) * 2), \
                patch("channels.send"):
            self.assertEqual(digest.run_once(self.cfg, self.db), 12)

    def test_failure_retries_only_unconfirmed_channels_even_after_feed_expiry(self):
        def fail_energy(cfg, body):
            if cfg["name"] == "energy":
                raise channels.DeliveryError("offline")

        with patch("channels.send", side_effect=fail_energy):
            with self.assertRaises(channels.DeliveryError):
                digest.run_once(self.cfg, self.db)
        with patch("feeds.fetch", return_value=[]), patch("channels.send") as send:
            self.assertEqual(digest.run_once(self.cfg, self.db), 9)
            self.assertEqual([call.args[0]["name"] for call in send.call_args_list],
                             ["energy", "everything"])

    def test_dry_run_does_not_record_observations_or_receipts(self):
        with patch("channels.send") as send, patch("builtins.print") as output:
            self.assertEqual(digest.run_once(self.cfg, self.db, dry_run=True), 0)
            send.assert_not_called()
            self.assertTrue(output.called)
            self.assertEqual(store.count_items(self.db), 0)
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
            self.assertEqual(digest.run_once(self.cfg, self.db), 12)
            output.reset_mock()
            digest.run_once(self.cfg, self.db, dry_run=True)
            output.assert_not_called()

    def test_channel_identity_and_filter_changes(self):
        with patch("channels.send"):
            digest.run_once(self.cfg, self.db)
            self.cfg["channels"][0]["keywords"] = []
            self.assertEqual(digest.run_once(self.cfg, self.db), 3)
            self.cfg["channels"] = [{"name": "new", "keywords": []}]
            self.assertEqual(digest.run_once(self.cfg, self.db), 6)

    def test_identity_ignores_edits_tracking_and_fragments_but_keeps_source_and_query(self):
        item = {"source": "wire", "link": "https://EXAMPLE.com/story?utm_source=a&id=1#top"}
        same = dict(item, link="https://example.com/story?id=1&utm_campaign=b")
        self.assertEqual(store.article_key(item), store.article_key(same))
        self.assertNotEqual(store.article_key(item), store.article_key(dict(same, source="other")))
        self.assertNotEqual(store.article_key(item),
                            store.article_key(dict(same, link="https://example.com/story?id=2")))

    def test_old_archive_is_preserved_but_does_not_imply_delivery(self):
        legacy_path = Path(self.tmp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as old, old:
            old.execute("CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "source TEXT NOT NULL, raw_id TEXT, title TEXT NOT NULL, "
                        "link TEXT NOT NULL, summary TEXT, published TEXT, "
                        "first_seen TEXT NOT NULL DEFAULT (datetime('now')))")
            old.execute("INSERT INTO items (source, title, link) VALUES ('old', 'Old', 'https://old')")
        upgraded = store.connect(legacy_path)
        self.addCleanup(upgraded.close)
        self.assertEqual(store.count_items(upgraded), 1)
        cfg = {"feeds": [], "channels": [{"name": "all"}]}
        with patch("channels.send"):
            self.assertEqual(digest.run_once(cfg, upgraded), 1)
            self.assertEqual(digest.run_once(cfg, upgraded), 0)

    def test_overlapping_runs_send_once(self):
        # Both runs reach the same archived backlog. The second must block on
        # selection until the first has committed its successful receipt.
        with patch("channels.send"), patch("builtins.print"):
            digest.run_once(self.cfg, self.db)
        self.cfg["channels"] = [{"name": "concurrent"}]
        entered = threading.Event()
        release = threading.Event()
        second_started = threading.Event()
        results = []
        errors = []

        def send(cfg, body):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timed out waiting to release send")

        def run(second=False):
            db = store.connect(self.path)
            try:
                if second:
                    second_started.set()
                results.append(digest.run_once({"feeds": [], "channels": self.cfg["channels"]}, db))
            except Exception as exc:
                errors.append(exc)
            finally:
                db.close()

        with patch("channels.send", side_effect=send) as delivery:
            first = threading.Thread(target=run)
            second = threading.Thread(target=run, args=(True,))
            first.start()
            try:
                self.assertTrue(entered.wait(3))
                second.start()
                self.assertTrue(second_started.wait(3))
            finally:
                release.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(sorted(results), [0, 6])
            delivery.assert_called_once()


if __name__ == "__main__":
    unittest.main()
