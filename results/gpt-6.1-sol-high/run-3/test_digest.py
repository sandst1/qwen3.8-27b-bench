"""Offline regression tests: python3 -m unittest -v."""

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


ROOT = Path(__file__).resolve().parent


def config(snapshot="a"):
    cfg = digest.load_config(ROOT / "config.example.toml")
    for feed in cfg["feeds"]:
        feed["url"] = str(ROOT / "fixtures" / f"snapshot-{snapshot}" /
                          f"{feed['name']}.json")
    return cfg


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "digest.sqlite3")

    def run_digest(self, cfg, dry_run=False):
        db = store.connect(self.path)
        try:
            return digest.run_once(cfg, db, dry_run=dry_run)
        finally:
            db.close()

    def test_repeat_and_updated_snapshots_across_connections(self):
        with patch("channels.send") as send:
            self.assertEqual(self.run_digest(config()), 12)
            self.assertEqual(send.call_count, 3)
            send.reset_mock()
            self.assertEqual(self.run_digest(config()), 0)
            send.assert_not_called()
            self.assertEqual(self.run_digest(config("b")), 2)
            self.assertEqual([c.args[0]["name"] for c in send.call_args_list],
                             ["ops", "everything"])
            for call in send.call_args_list:
                self.assertIn("1 item(s)", call.args[1])
                self.assertIn("Union responds", call.args[1])
            send.reset_mock()
            self.assertEqual(self.run_digest(config("b")), 0)
            send.assert_not_called()

    def test_channel_failure_preserves_previous_success(self):
        def fail_energy(channel, body):
            if channel["name"] == "energy":
                raise channels.DeliveryError("offline")

        with patch("channels.send", side_effect=fail_energy):
            with self.assertRaises(channels.DeliveryError):
                self.run_digest(config())
        with patch("channels.send") as send:
            self.assertEqual(self.run_digest(config()), 9)
            self.assertEqual([c.args[0]["name"] for c in send.call_args_list],
                             ["energy", "everything"])

    def test_dry_run_previews_pending_without_consuming_deliveries(self):
        with patch("channels.send") as send, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.run_digest(config(), dry_run=True), 0)
            self.assertIn("would send to ops", out.getvalue())
            send.assert_not_called()
            self.assertEqual(self.run_digest(config()), 12)
            out.seek(0)
            out.truncate()
            self.assertEqual(self.run_digest(config(), dry_run=True), 0)
            self.assertEqual(out.getvalue(), "")

    def test_new_channel_and_changed_filter_have_independent_history(self):
        cfg = config()
        cfg["channels"] = [cfg["channels"][0]]
        with patch("channels.send") as send:
            self.assertEqual(self.run_digest(cfg), 3)
            cfg["channels"][0]["keywords"] = []
            self.assertEqual(self.run_digest(cfg), 3)
            cfg["channels"].append(dict(cfg["channels"][0], name="new"))
            self.assertEqual(self.run_digest(cfg), 6)
            self.assertEqual(send.call_args.args[0]["name"], "new")

    def test_duplicates_in_one_poll_only_sent_once_per_channel(self):
        cfg = config()
        cfg["feeds"].append(copy.deepcopy(cfg["feeds"][0]))
        with patch("channels.send"):
            self.assertEqual(self.run_digest(cfg), 12)
            self.assertEqual(self.run_digest(cfg), 0)

    def test_legacy_archive_is_not_treated_as_delivery_history(self):
        # An old database has observations, but cannot tell us who received them.
        db = sqlite3.connect(self.path)
        db.executescript("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
                raw_id TEXT, title TEXT NOT NULL, link TEXT NOT NULL,
                summary TEXT, published TEXT,
                first_seen TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO items (source, raw_id, title, link)
            VALUES ('wire', 'old-guid', 'Old title', 'https://wire.example/i/0031');
        """)
        db.close()
        with patch("channels.send"):
            self.assertEqual(self.run_digest(config()), 12)
            self.assertEqual(self.run_digest(config()), 0)
        db = store.connect(self.path)
        try:
            row = db.execute("SELECT title FROM items WHERE id = 1").fetchone()
            self.assertEqual(row["title"], "Old title")
        finally:
            db.close()

    def test_overlapping_runs_do_not_both_send(self):
        # Separate connections model separate cron processes. Pause the first
        # sender while the second starts, then let both finish normally.
        sending = threading.Event()
        release = threading.Event()
        second_started = threading.Event()
        cfg = config()
        cfg["channels"] = [cfg["channels"][2]]

        def send(channel, body):
            sending.set()
            if not release.wait(10):
                raise RuntimeError("test did not release sender")

        def second_run():
            second_started.set()
            return self.run_digest(cfg)

        with patch("channels.send", side_effect=send) as sender:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.run_digest, cfg)
                try:
                    self.assertTrue(sending.wait(10))
                    second = pool.submit(second_run)
                    self.assertTrue(second_started.wait(10))
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=10), 6)
                self.assertEqual(second.result(timeout=10), 0)
            self.assertEqual(sender.call_count, 1)

    def test_failed_feed_does_not_erase_delivery_history(self):
        with patch("channels.send") as send:
            self.assertEqual(self.run_digest(config()), 12)
            cfg = config()
            cfg["feeds"][0]["url"] = str(Path(self.tmp.name) / "missing.json")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(self.run_digest(cfg), 0)
            self.assertIn("warn: feed newsroom failed", err.getvalue())
            send.reset_mock()
            self.assertEqual(self.run_digest(config()), 0)
            send.assert_not_called()


class IdentityTests(unittest.TestCase):
    def key(self, link):
        return store.item_key({"link": link})

    def test_tracking_and_fragments_do_not_change_identity(self):
        self.assertEqual(
            self.key("HTTPS://EXAMPLE.COM/article?id=3&utm_campaign=one&fbclid=x#top"),
            self.key("https://example.com/article?id=3&utm_source=two&gclid=y"),
        )

    def test_meaningful_url_differences_remain_distinct(self):
        self.assertNotEqual(self.key("https://example.com/?id=1"),
                            self.key("https://example.com/?id=2"))
        self.assertNotEqual(self.key("https://example.com/a"),
                            self.key("https://example.com/b"))

    def test_same_url_from_different_sources_is_distinct(self):
        cfg = config()
        cfg["channels"] = [cfg["channels"][2]]
        item = {"title": "Same", "link": "https://example.com/article",
                "raw_id": None, "source": "one"}
        with tempfile.TemporaryDirectory() as tmp:
            db = store.connect(str(Path(tmp) / "digest.sqlite3"))
            try:
                with patch("feeds.fetch", side_effect=lambda f: [dict(item, source=f["name"])]), \
                        patch("channels.send"):
                    self.assertEqual(digest.run_once(cfg, db), 3)
                    self.assertEqual(digest.run_once(cfg, db), 0)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
