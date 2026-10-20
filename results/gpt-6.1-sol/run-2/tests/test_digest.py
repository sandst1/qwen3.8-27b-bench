import contextlib
import copy
import io
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import channels
import digest
import feeds
import store


ROOT = Path(__file__).resolve().parents[1]


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = str(Path(self.temp.name) / "digest.sqlite3")
        self.cfg = digest.load_config(ROOT / "config.example.toml")
        self.snapshot("snapshot-a")

    def snapshot(self, name):
        for feed in self.cfg["feeds"]:
            feed["url"] = str(ROOT / "fixtures" / name / f"{feed['name']}.json")

    def run_digest(self, cfg=None, dry_run=False):
        # A new connection models independent cron invocations.
        with contextlib.closing(store.connect(self.db_path)) as db:
            return digest.run_once(cfg or self.cfg, db, dry_run=dry_run)

    def test_snapshots_and_persistent_receipts(self):
        with patch("channels.send") as send:
            self.assertEqual(self.run_digest(), 12)
            self.assertEqual(send.call_count, 3)
            send.reset_mock()
            self.assertEqual(self.run_digest(), 0)
            send.assert_not_called()
            self.snapshot("snapshot-b")
            self.assertEqual(self.run_digest(), 2)
            self.assertEqual([c.args[0]["name"] for c in send.call_args_list],
                             ["ops", "everything"])
            for call in send.call_args_list:
                self.assertIn("Union responds", call.args[1])
                self.assertIn("1 item(s)", call.args[1])
            send.reset_mock()
            self.assertEqual(self.run_digest(), 0)
            send.assert_not_called()

    def test_failed_channel_retries_without_repeating_successes(self):
        def fail_energy(cfg, body):
            if cfg["name"] == "energy":
                raise channels.DeliveryError("offline")

        with patch("channels.send", side_effect=fail_energy):
            with self.assertRaises(channels.DeliveryError):
                self.run_digest()
        with patch("channels.send") as send:
            self.assertEqual(self.run_digest(), 9)
            self.assertEqual([c.args[0]["name"] for c in send.call_args_list],
                             ["energy", "everything"])

    def test_dry_run_does_not_consume_items(self):
        with patch("channels.send") as send, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.run_digest(dry_run=True), 0)
            self.assertIn("would send to ops", out.getvalue())
            send.assert_not_called()
            self.assertEqual(self.run_digest(), 12)
            send.reset_mock()
            out.seek(0)
            out.truncate()
            self.assertEqual(self.run_digest(dry_run=True), 0)
            self.assertEqual(out.getvalue(), "")
            send.assert_not_called()

    def test_duplicates_in_one_poll(self):
        original_fetch = feeds.fetch

        def repeated(feed):
            items = original_fetch(feed)
            return items + copy.deepcopy(items)

        with patch("feeds.fetch", side_effect=repeated), patch("channels.send") as send:
            self.assertEqual(self.run_digest(), 12)
            self.assertIn("6 item(s)", send.call_args_list[-1].args[1])

    def test_filter_changes_and_new_channels_have_independent_history(self):
        self.cfg["channels"] = [self.cfg["channels"][0]]
        with patch("channels.send"):
            self.assertEqual(self.run_digest(), 3)
            self.cfg["channels"][0]["keywords"] = []
            self.assertEqual(self.run_digest(), 3)
            self.cfg["channels"].append({"name": "new", "keywords": []})
            self.assertEqual(self.run_digest(), 6)
            self.assertEqual(self.run_digest(), 0)

    def test_existing_archive_is_preserved_but_not_treated_as_delivery(self):
        # Original schema, including duplicate archive observations.
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("""CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
                raw_id TEXT, title TEXT NOT NULL, link TEXT NOT NULL,
                summary TEXT, published TEXT,
                first_seen TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
            for _ in range(2):
                db.execute("INSERT INTO items (source, title, link) VALUES (?, ?, ?)",
                           ("newsroom", "old observation", "https://newsroom.example/old"))
        with patch("channels.send"):
            self.assertEqual(self.run_digest(), 12)
            self.assertEqual(self.run_digest(), 0)
        with contextlib.closing(store.connect(self.db_path)) as db:
            self.assertEqual(store.count_items(db), 14)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM items WHERE title = 'old observation'"
            ).fetchone()[0], 2)

    def test_feed_failure_does_not_consume_missing_items(self):
        original_fetch = feeds.fetch

        def fail_wire(feed):
            if feed["name"] == "wire":
                raise feeds.FeedError("offline")
            return original_fetch(feed)

        with patch("channels.send"), patch("feeds.fetch", side_effect=fail_wire), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_digest(), 8)
        with patch("channels.send"):
            self.assertEqual(self.run_digest(), 4)

    def test_overlapping_runs_send_each_channel_once(self):
        first_sending = threading.Event()
        second_fetching = threading.Event()
        results = []
        errors = []
        original_fetch = feeds.fetch

        def fetch(feed):
            if threading.current_thread().name == "second":
                second_fetching.set()
            return original_fetch(feed)

        def send(cfg, body):
            if threading.current_thread().name == "first" and cfg["name"] == "ops":
                first_sending.set()
                if not second_fetching.wait(5):
                    raise AssertionError("second run did not start")

        def run():
            try:
                results.append(self.run_digest())
            except BaseException as exc:
                errors.append(exc)

        with patch("feeds.fetch", side_effect=fetch), patch("channels.send", side_effect=send) as sent:
            first = threading.Thread(target=run, name="first")
            second = threading.Thread(target=run, name="second")
            first.start()
            try:
                self.assertTrue(first_sending.wait(5))
                second.start()
            finally:
                if second.ident is None:
                    second_fetching.set()
                first.join(10)
                if second.ident is not None:
                    second.join(10)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(sum(results), 12)
            self.assertEqual(sent.call_count, 3)


class IdentityTests(unittest.TestCase):
    def key(self, link, source="wire", **edits):
        return store.item_key({"source": source, "link": link, **edits})

    def test_tracking_and_content_edits_do_not_change_identity(self):
        self.assertEqual(
            self.key("HTTPS://EXAMPLE.COM/story?b=2&utm_campaign=one&a=1#top"),
            self.key("https://example.com/story?a=1&gclid=ad&b=2&fbclid=ad&utm_source=x",
                     title="edited", raw_id="new-guid"),
        )

    def test_meaningful_query_path_and_source_remain_distinct(self):
        original = self.key("https://example.com/story?id=1")
        for link, source in [
            ("https://example.com/story?id=2", "wire"),
            ("https://example.com/other?id=1", "wire"),
            ("https://example.com/story?id=1", "newsroom"),
        ]:
            with self.subTest(link=link, source=source):
                self.assertNotEqual(original, self.key(link, source))


if __name__ == "__main__":
    unittest.main()
