import contextlib
import copy
import io
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import channels
import digest
import store


ROOT = Path(__file__).resolve().parents[1]


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "digest.sqlite3"
        self.db = store.connect(self.path)
        self.addCleanup(self.db.close)
        self.cfg = digest.load_config(ROOT / "config.example.toml")
        self.snapshot("snapshot-a")

    def snapshot(self, name):
        for feed in self.cfg["feeds"]:
            feed["url"] = str(ROOT / "fixtures" / name / (feed["name"] + ".json"))

    def test_repeat_poll_and_edits_only_send_new_articles(self):
        with patch("channels.send") as send:
            self.assertEqual(digest.run_once(self.cfg, self.db), 12)
            self.assertEqual(send.call_count, 3)
            # Reopen to verify persistence across cron invocations.
            with contextlib.closing(store.connect(self.path)) as db:
                self.assertEqual(digest.run_once(self.cfg, db), 0)
            self.assertEqual(send.call_count, 3)
            self.snapshot("snapshot-b")
            self.assertEqual(digest.run_once(self.cfg, self.db), 2)
            self.assertEqual(send.call_count, 5)
            for call in send.call_args_list[3:]:
                self.assertIn("Union responds", call.args[1])
                self.assertIn("1 item(s)", call.args[1])
            self.assertEqual(digest.run_once(self.cfg, self.db), 0)

    def test_partial_failure_preserves_success_and_retries_unsent_channels(self):
        def fail_second(cfg, body):
            if cfg["name"] == "energy":
                raise channels.DeliveryError("offline")

        with patch("channels.send", side_effect=fail_second) as send:
            with self.assertRaises(channels.DeliveryError):
                digest.run_once(self.cfg, self.db)
            self.assertEqual([c.args[0]["name"] for c in send.call_args_list],
                             ["ops", "energy"])
        with patch("channels.send") as send:
            self.assertEqual(digest.run_once(self.cfg, self.db), 9)
            self.assertEqual([c.args[0]["name"] for c in send.call_args_list],
                             ["energy", "everything"])
            self.assertEqual(digest.run_once(self.cfg, self.db), 0)

    def test_dry_run_does_not_consume_pending_items(self):
        with patch("channels.send") as send, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(digest.run_once(self.cfg, self.db, dry_run=True), 0)
            self.assertIn("would send to ops", output.getvalue())
            send.assert_not_called()
            self.assertEqual(digest.run_once(self.cfg, self.db), 12)
            output.truncate(0)
            output.seek(0)
            self.assertEqual(digest.run_once(self.cfg, self.db, dry_run=True), 0)
            self.assertEqual(output.getvalue(), "")

    def test_new_channel_and_filter_changes_have_independent_history(self):
        self.cfg["channels"] = [self.cfg["channels"][0]]
        with patch("channels.send"):
            self.assertEqual(digest.run_once(self.cfg, self.db), 3)
            self.cfg["channels"][0]["keywords"] = []
            self.assertEqual(digest.run_once(self.cfg, self.db), 3)
            new_channel = copy.deepcopy(self.cfg["channels"][0])
            new_channel["name"] = "new"
            self.cfg["channels"].append(new_channel)
            self.assertEqual(digest.run_once(self.cfg, self.db), 6)

    def test_identity_deduplicates_tracking_and_edits_but_preserves_article_queries(self):
        item = dict(source="test", raw_id=None, title="port", summary="",
                    link="https://example.test/story?id=1&utm_source=feed#top")
        edited = dict(item, raw_id="changed", title="port corrected",
                      link="https://example.test/story?id=1&utm_source=other#bottom")
        other_article = dict(item, link="https://example.test/story?id=2")
        other_source = dict(item, source="other")
        self.cfg["feeds"] = [dict(name="test")]
        self.cfg["channels"] = [dict(name="all", keywords=[])]
        with patch("feeds.fetch", return_value=[item, edited, other_article, other_source]), \
                patch("channels.send") as send:
            self.assertEqual(digest.run_once(self.cfg, self.db), 3)
            self.assertIn("3 item(s)", send.call_args.args[1])
            self.assertEqual(digest.run_once(self.cfg, self.db), 0)

    def test_existing_archive_is_preserved_and_not_assumed_delivered(self):
        legacy_path = Path(self.tmp.name) / "legacy.sqlite3"
        with contextlib.closing(sqlite3.connect(legacy_path)) as legacy:
            legacy.executescript("""
                CREATE TABLE items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
                    raw_id TEXT, title TEXT NOT NULL, link TEXT NOT NULL,
                    summary TEXT, published TEXT,
                    first_seen TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO items (source, title, link)
                VALUES ('legacy', 'Old observation', 'https://example.test/old');
            """)
        with contextlib.closing(store.connect(legacy_path)) as db, patch("channels.send"):
            self.assertEqual(store.count_items(db), 1)
            self.assertEqual(digest.run_once(self.cfg, db), 12)
            self.assertEqual(store.count_items(db), 7)
            self.assertEqual(digest.run_once(self.cfg, db), 0)

    def test_overlapping_runs_do_not_both_deliver(self):
        sending = threading.Event()
        release = threading.Event()
        second_fetching = threading.Event()

        def send(cfg, body):
            sending.set()
            if not release.wait(10):
                raise RuntimeError("timed out waiting for overlapping run")

        def run(second=False):
            with contextlib.closing(store.connect(self.path)) as db:
                if second:
                    second_fetching.set()
                return digest.run_once(self.cfg, db)

        # Both workers use separate connections to the same on-disk database.
        with patch("channels.send", side_effect=send) as delivery, \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run)
            try:
                self.assertTrue(sending.wait(10))
                second = pool.submit(run, True)
                self.assertTrue(second_fetching.wait(10))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10), 12)
            self.assertEqual(second.result(timeout=10), 0)
            self.assertEqual(delivery.call_count, 3)


if __name__ == "__main__":
    unittest.main()
