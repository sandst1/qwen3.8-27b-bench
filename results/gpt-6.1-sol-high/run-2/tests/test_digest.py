"""Offline delivery regressions. Run: python3 -m unittest discover -s tests -v."""

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
import feeds
import store


ROOT = Path(__file__).resolve().parents[1]


def config(snapshot="snapshot-a"):
    cfg = digest.load_config(ROOT / "config.example.toml")
    for feed in cfg["feeds"]:
        feed["url"] = str(ROOT / "fixtures" / snapshot / f"{feed['name']}.json")
    return cfg


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "digest.sqlite3"
        self.db = store.connect(self.path)
        self.addCleanup(lambda: self.db.close())

    def test_snapshots_and_restart(self):
        with patch("channels.send") as send:
            self.assertEqual(digest.run_once(config(), self.db), 12)
            self.assertEqual(send.call_count, 3)
            self.db.close()
            self.db = store.connect(self.path)
            send.reset_mock()
            self.assertEqual(digest.run_once(config(), self.db), 0)
            send.assert_not_called()
            self.assertEqual(digest.run_once(config("snapshot-b"), self.db), 2)
            self.assertEqual([c.args[0]["name"] for c in send.call_args_list],
                             ["ops", "everything"])
            for call in send.call_args_list:
                self.assertIn("Union responds to port fee inquiry", call.args[1])
                self.assertIn("1 item(s)", call.args[1])
            self.assertEqual(digest.run_once(config("snapshot-b"), self.db), 0)

    def test_successful_channels_survive_later_failure(self):
        def fail_energy(channel, body):
            if channel["name"] == "energy":
                raise channels.DeliveryError("offline")

        with patch("channels.send", side_effect=fail_energy):
            with self.assertRaises(channels.DeliveryError):
                digest.run_once(config(), self.db)
        self.db.close()
        self.db = store.connect(self.path)
        with patch("channels.send") as send:
            self.assertEqual(digest.run_once(config(), self.db), 9)
            self.assertEqual([c.args[0]["name"] for c in send.call_args_list],
                             ["energy", "everything"])
            self.assertEqual(digest.run_once(config(), self.db), 0)

    def test_dry_run_previews_pending_without_consuming(self):
        with patch("channels.send") as send, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(digest.run_once(config(), self.db, dry_run=True), 0)
            self.assertIn("would send to ops", output.getvalue())
            send.assert_not_called()
            self.assertEqual(digest.run_once(config(), self.db), 12)
            output.seek(0)
            output.truncate()
            self.assertEqual(digest.run_once(config(), self.db, dry_run=True), 0)
            self.assertEqual(output.getvalue(), "")

    def test_channel_filter_changes_and_new_channels(self):
        cfg = config()
        cfg["channels"] = [cfg["channels"][0]]
        with patch("channels.send"):
            self.assertEqual(digest.run_once(cfg, self.db), 3)
            cfg["channels"][0]["keywords"] = []
            self.assertEqual(digest.run_once(cfg, self.db), 3)
            cfg["channels"][0]["name"] = "new-channel"
            self.assertEqual(digest.run_once(cfg, self.db), 6)

    def test_duplicate_batch_and_source_scoping(self):
        item = feeds.fetch(config()["feeds"][0])[0]
        edited = dict(item, raw_id="changed", title="Edited title",
                      link=item["link"].replace("w33", "w34") + "#top")
        other_source = dict(item, source="another-feed")
        cfg = {"feeds": [{"name": "test"}],
               "channels": [{"name": "all", "keywords": []}]}
        with patch("feeds.fetch", return_value=[item, edited, other_source]), patch("channels.send") as send:
            self.assertEqual(digest.run_once(cfg, self.db), 2)
            self.assertIn("2 item(s)", send.call_args.args[1])
            self.assertEqual(digest.run_once(cfg, self.db), 0)

    def test_old_archive_is_not_delivery_history(self):
        self.db.close()
        with contextlib.closing(sqlite3.connect(self.path)) as old:
            old.execute("DROP TABLE deliveries")
            for feed in config()["feeds"]:
                store.record_items(old, feed["name"], feeds.fetch(feed))
        self.db = store.connect(self.path)
        self.assertEqual(store.count_items(self.db), 6)
        with patch("channels.send"):
            self.assertEqual(digest.run_once(config(), self.db), 12)
            self.assertEqual(digest.run_once(config(), self.db), 0)

    def test_feed_failure_does_not_consume_its_items(self):
        fetch = feeds.fetch

        def fail_wire(feed):
            if feed["name"] == "wire":
                raise feeds.FeedError("offline")
            return fetch(feed)

        with patch("channels.send"), patch("feeds.fetch", side_effect=fail_wire), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(digest.run_once(config(), self.db), 8)
        with patch("channels.send"):
            self.assertEqual(digest.run_once(config(), self.db), 4)

    def test_overlapping_process_connections_send_only_once(self):
        # Separate connections, as cron processes would use. Block the first
        # sender while the second invocation starts fetching the same snapshot.
        sending = threading.Event()
        second_fetching = threading.Event()
        release = threading.Event()
        sent = []
        cfg = config()
        cfg["channels"] = [cfg["channels"][-1]]

        def blocking_send(channel, body):
            sent.append(body)
            sending.set()
            if not release.wait(5):
                raise RuntimeError("test did not release sender")

        def run(second=False):
            db = store.connect(self.path)
            try:
                if second:
                    second_fetching.set()
                return digest.run_once(copy.deepcopy(cfg), db)
            finally:
                db.close()

        with patch("channels.send", side_effect=blocking_send), \
                ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(run)
            try:
                self.assertTrue(sending.wait(5))
                second = workers.submit(run, True)
                self.assertTrue(second_fetching.wait(5))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10), 6)
            self.assertEqual(second.result(timeout=10), 0)
        self.assertEqual(len(sent), 1)


class IdentityTests(unittest.TestCase):
    def test_tracking_and_query_order_are_ignored(self):
        def key(link):
            return store.item_key({"link": link})

        self.assertEqual(
            key("HTTPS://Example.COM/article?b=2&utm_campaign=new&a=1&gclid=x&fbclid=y#section"),
            key("https://example.com/article?a=1&b=2"),
        )

    def test_article_query_values_and_paths_remain_distinct(self):
        links = ["https://example.com/article?id=1", "https://example.com/article?id=2",
                 "https://example.com/article?id=", "https://example.com/article",
                 "https://example.com/Article", "https://example.com/article/",
                 "http://example.com/article"]
        self.assertEqual(len({store.item_key({"link": link}) for link in links}), len(links))


if __name__ == "__main__":
    unittest.main()
