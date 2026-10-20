"""Regression checks: python3 -m unittest -v (no network or real delivery)."""

import contextlib
import copy
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import channels
import digest
import feeds
import store


ROOT = Path(__file__).resolve().parent


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

    def test_repeat_across_connections_and_edited_snapshot(self):
        with patch("channels.send") as send:
            self.assertEqual(digest.run_once(self.cfg, self.db), 12)
            self.assertEqual(send.call_count, 3)
            # Persisted state, not a process-local cache.
            with contextlib.closing(store.connect(self.path)) as db:
                self.assertEqual(digest.run_once(self.cfg, db), 0)
            self.assertEqual(send.call_count, 3)
            self.snapshot("snapshot-b")
            send.reset_mock()
            self.assertEqual(digest.run_once(self.cfg, self.db), 2)
            self.assertEqual([call.args[0]["name"] for call in send.call_args_list],
                             ["ops", "everything"])
            for call in send.call_args_list:
                self.assertIn("Union responds to port fee inquiry", call.args[1])
                self.assertIn("1 item(s)", call.args[1])
            self.assertEqual(digest.run_once(self.cfg, self.db), 0)

    def test_duplicate_rows_in_one_poll(self):
        original = feeds.fetch
        with patch("feeds.fetch", side_effect=lambda cfg: original(cfg) * 2), \
                patch("channels.send"):
            self.assertEqual(digest.run_once(self.cfg, self.db), 12)

    def test_dry_run_does_not_record_delivery(self):
        with patch("channels.send") as send, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(digest.run_once(self.cfg, self.db, dry_run=True), 0)
            send.assert_not_called()
            self.assertIn("would send to ops", out.getvalue())
            self.assertEqual(digest.run_once(self.cfg, self.db), 12)
            out.truncate(0)
            out.seek(0)
            digest.run_once(self.cfg, self.db, dry_run=True)
            self.assertEqual(out.getvalue(), "")

    def test_failure_keeps_previous_channel_receipts(self):
        def fail_energy(cfg, body):
            if cfg["name"] == "energy":
                raise channels.DeliveryError("offline")

        with patch("channels.send", side_effect=fail_energy):
            with self.assertRaises(channels.DeliveryError):
                digest.run_once(self.cfg, self.db)
        with patch("channels.send") as send:
            self.assertEqual(digest.run_once(self.cfg, self.db), 9)
            self.assertEqual([call.args[0]["name"] for call in send.call_args_list],
                             ["energy", "everything"])
            self.assertEqual(digest.run_once(self.cfg, self.db), 0)

    def test_filter_changes_and_new_channel_have_independent_history(self):
        self.cfg["channels"] = [self.cfg["channels"][0]]
        with patch("channels.send"):
            self.assertEqual(digest.run_once(self.cfg, self.db), 3)
            self.cfg["channels"][0]["keywords"] = []
            self.assertEqual(digest.run_once(self.cfg, self.db), 3)
            new_channel = copy.deepcopy(self.cfg["channels"][0])
            new_channel["name"] = "new"
            self.cfg["channels"].append(new_channel)
            self.assertEqual(digest.run_once(self.cfg, self.db), 6)

    def test_feed_failure_does_not_mark_missing_items(self):
        original = feeds.fetch

        def unavailable(cfg):
            if cfg["name"] == "newsroom":
                raise feeds.FeedError("offline")
            return original(cfg)

        with patch("channels.send"), patch("feeds.fetch", side_effect=unavailable), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(digest.run_once(self.cfg, self.db), 8)
        with patch("channels.send"):
            self.assertEqual(digest.run_once(self.cfg, self.db), 4)

    def test_existing_archive_is_not_treated_as_delivery(self):
        # Simulate an old database: only the observation archive exists.
        self.db.execute("DROP TABLE deliveries")
        store.record_items(self.db, "newsroom", feeds.fetch(self.cfg["feeds"][0]))
        with contextlib.closing(store.connect(self.path)) as migrated, patch("channels.send"):
            self.assertEqual(store.count_items(migrated), 2)
            self.assertEqual(digest.run_once(self.cfg, migrated), 12)
            self.assertEqual(digest.run_once(self.cfg, migrated), 0)

    def test_overlapping_runs_serialize_delivery(self):
        sending = threading.Event()
        release = threading.Event()
        second_started = threading.Event()

        def send(cfg, body):
            if not sending.is_set():
                sending.set()
                if not release.wait(3):
                    raise RuntimeError("test timed out waiting for release")

        def run(started=None):
            with contextlib.closing(store.connect(self.path)) as db:
                if started:
                    started.set()
                return digest.run_once(self.cfg, db)

        with patch("channels.send", side_effect=send) as delivery, \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run)
            try:
                self.assertTrue(sending.wait(3))
                second = pool.submit(run, second_started)
                self.assertTrue(second_started.wait(3))
            finally:
                release.set()
            self.assertEqual(first.result() + second.result(), 12)
            self.assertEqual(delivery.call_count, 3)


class IdentityTests(unittest.TestCase):
    def identity(self, link, raw_id=None, fmt="generic", **extra):
        return feeds.item_identity(dict(link=link, raw_id=raw_id, **extra), {"format": fmt})

    def test_url_tracking_edits_and_query_order(self):
        self.assertEqual(
            self.identity("https://EXAMPLE.test/story?b=2&utm_source=feed&a=1#top", "old"),
            self.identity("https://example.test/story?a=1&b=2&gclid=ad", "new",
                          title="Edited", summary="New summary"),
        )
        self.assertNotEqual(self.identity("https://example.test/?id=1"),
                            self.identity("https://example.test/?id=2"))

    def test_newsroom_id_survives_link_change(self):
        self.assertEqual(self.identity("https://example.test/old", "1", "newsroom"),
                         self.identity("https://example.test/new", "1", "newsroom"))
        self.assertNotEqual(self.identity("https://example.test/old", "1", "newsroom"),
                            self.identity("https://example.test/old", "2", "newsroom"))


if __name__ == "__main__":
    unittest.main()
