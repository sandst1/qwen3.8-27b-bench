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
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "digest.sqlite3"
        self.db = store.connect(self.path)
        self.addCleanup(self.db.close)
        self.cfg = digest.load_config(ROOT / "config.example.toml")
        self.snapshot("a")

    def snapshot(self, name):
        for feed in self.cfg["feeds"]:
            filename = Path(feed["url"]).name
            feed["url"] = str(ROOT / "fixtures" / f"snapshot-{name}" / filename)

    def test_repeat_and_changed_snapshots_survive_restart(self):
        with patch("channels.send") as send:
            self.assertEqual(digest.run_once(self.cfg, self.db), 12)
            self.assertEqual(send.call_count, 3)
        # A new connection models the next cron process, not in-memory state.
        with contextlib.closing(store.connect(self.path)) as db:
            with patch("channels.send") as send:
                self.assertEqual(digest.run_once(self.cfg, db), 0)
                send.assert_not_called()
                self.snapshot("b")
                self.assertEqual(digest.run_once(self.cfg, db), 2)
                self.assertEqual(
                    [call.args[0]["name"] for call in send.call_args_list],
                    ["ops", "everything"],
                )
                for call in send.call_args_list:
                    self.assertIn("Union responds", call.args[1])
                    self.assertIn("1 item(s)", call.args[1])

    def test_partial_failure_preserves_success_and_retries_failed_channel(self):
        def fail_energy(cfg, body):
            if cfg["name"] == "energy":
                raise channels.DeliveryError("offline")

        with patch("channels.send", side_effect=fail_energy):
            with self.assertRaises(channels.DeliveryError):
                digest.run_once(self.cfg, self.db)
        with patch("channels.send") as send:
            self.assertEqual(digest.run_once(self.cfg, self.db), 9)
            self.assertEqual(
                [call.args[0]["name"] for call in send.call_args_list],
                ["energy", "everything"],
            )
            self.assertEqual(digest.run_once(self.cfg, self.db), 0)

    def test_dry_run_previews_only_unsent_without_marking(self):
        with patch("channels.send") as send:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(digest.run_once(self.cfg, self.db, dry_run=True), 0)
            self.assertIn("would send to ops", output.getvalue())
            send.assert_not_called()
            self.assertEqual(digest.run_once(self.cfg, self.db), 12)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                digest.run_once(self.cfg, self.db, dry_run=True)
            self.assertEqual(output.getvalue(), "")

    def test_channel_history_is_independent_of_other_channels_and_filters(self):
        self.cfg["channels"] = [self.cfg["channels"][0]]
        with patch("channels.send") as send:
            self.assertEqual(digest.run_once(self.cfg, self.db), 3)
            self.cfg["channels"][0]["keywords"] = []
            self.assertEqual(digest.run_once(self.cfg, self.db), 3)
            new_channel = dict(self.cfg["channels"][0], name="new")
            self.cfg["channels"].append(new_channel)
            self.assertEqual(digest.run_once(self.cfg, self.db), 6)
            self.assertEqual(send.call_args.args[0]["name"], "new")

    def test_feed_failure_does_not_mark_missing_items_delivered(self):
        original = self.cfg["feeds"][0]["url"]
        self.cfg["feeds"][0]["url"] = str(Path(self.temp.name) / "missing.json")
        with patch("channels.send"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(digest.run_once(self.cfg, self.db), 8)
        self.cfg["feeds"][0]["url"] = original
        with patch("channels.send"):
            self.assertEqual(digest.run_once(self.cfg, self.db), 4)

    def test_duplicate_batch_and_source_scoping(self):
        item = {
            "source": "one", "raw_id": None, "title": "Article",
            "link": "https://example.test/article?utm_source=a#top",
        }
        edited = dict(item, raw_id="new", title="Retitled",
                      link="https://example.test/article?utm_source=b")
        other_source = dict(edited, source="two")
        self.cfg["feeds"] = [{"name": "one"}]
        self.cfg["channels"] = [{"name": "all", "keywords": []}]
        with patch("feeds.fetch", return_value=[item, edited, other_source]):
            with patch("channels.send") as send:
                self.assertEqual(digest.run_once(self.cfg, self.db), 2)
                self.assertIn("2 item(s)", send.call_args.args[1])
                self.assertEqual(digest.run_once(self.cfg, self.db), 0)

    def test_url_identity_retains_article_parameters(self):
        def key(link):
            return store.item_key({"source": "feed", "link": link})

        self.assertEqual(
            key("https://example.test/read?id=1&UTM_campaign=a#section"),
            key("https://example.test/read?id=1"),
        )
        self.assertNotEqual(
            key("https://example.test/read?id=1"),
            key("https://example.test/read?id=2"),
        )

    def test_existing_archive_is_preserved_but_not_assumed_delivered(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        with contextlib.closing(sqlite3.connect(legacy_path)) as legacy:
            legacy.executescript("""
                CREATE TABLE items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
                    raw_id TEXT, title TEXT NOT NULL, link TEXT NOT NULL,
                    summary TEXT, published TEXT,
                    first_seen TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO items (source, title, link)
                VALUES ('old', 'Archived', 'https://example.test/old');
            """)
        with contextlib.closing(store.connect(legacy_path)) as db:
            self.assertEqual(store.count_items(db), 1)
            with patch("channels.send"):
                self.assertEqual(digest.run_once(self.cfg, db), 12)
                self.assertEqual(digest.run_once(self.cfg, db), 0)
            self.assertEqual(store.count_items(db), 13)

    def test_overlapping_runs_send_once(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["channels"] = [cfg["channels"][2]]
        sending = threading.Event()
        release = threading.Event()
        second_fetching = threading.Event()

        def send(cfg, body):
            sending.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test did not release sender")

        def run(second=False):
            with contextlib.closing(store.connect(self.path)) as db:
                if second:
                    second_fetching.set()
                return digest.run_once(cfg, db)

        with patch("channels.send", side_effect=send) as sender:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(run)
                try:
                    self.assertTrue(sending.wait(timeout=5))
                    second = pool.submit(run, True)
                    self.assertTrue(second_fetching.wait(timeout=5))
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=5), 6)
                self.assertEqual(second.result(timeout=5), 0)
            sender.assert_called_once()


if __name__ == "__main__":
    unittest.main()
