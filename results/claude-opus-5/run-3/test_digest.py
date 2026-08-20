"""Regression tests for digest deduplication.

Run: python3 test_digest.py

These exist because the job's failure mode is silent and slow: it looks fine
for one run and then spams the same items every 15 minutes forever. The
snapshot-a -> snapshot-b transition in fixtures/ is the interesting case, since
each feed mutates in a way that defeats a different naive dedup key.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import digest
import feeds
import store

FIXTURES = Path(__file__).parent / "fixtures"


def config(snapshot, channels=None):
    return {
        "feeds": [
            {"name": "newsroom", "format": "newsroom",
             "url": f"file://{FIXTURES}/{snapshot}/newsroom.json"},
            {"name": "blogroll", "format": "blogroll",
             "url": f"file://{FIXTURES}/{snapshot}/blogroll.json"},
            {"name": "wire", "format": "generic",
             "url": f"file://{FIXTURES}/{snapshot}/wire.json"},
        ],
        "channels": channels if channels is not None else [
            {"name": "ops", "type": "stdout", "keywords": ["port", "levy", "fee"]},
            {"name": "energy", "type": "stdout",
             "keywords": ["tender", "grid", "offshore"]},
            {"name": "everything", "type": "stdout", "keywords": []},
        ],
    }


class IdentityTests(unittest.TestCase):
    def test_tracking_params_stripped(self):
        """newsroom restamps utm_campaign every week; the article is unchanged."""
        a = feeds.identity({"source": "n", "link": "https://x.example/a?utm_campaign=w33"})
        b = feeds.identity({"source": "n", "link": "https://x.example/a?utm_campaign=w34"})
        self.assertEqual(a, b)

    def test_meaningful_query_params_kept(self):
        """Stripping must not collapse genuinely distinct urls."""
        a = feeds.identity({"source": "n", "link": "https://x.example/view?id=1"})
        b = feeds.identity({"source": "n", "link": "https://x.example/view?id=2"})
        self.assertNotEqual(a, b)

    def test_unstable_guid_ignored(self):
        """wire appends -r2 to its guid on edit; same link, same story."""
        a = feeds.identity({"source": "w", "link": "https://w.example/i/1",
                            "raw_id": "x-0031"})
        b = feeds.identity({"source": "w", "link": "https://w.example/i/1",
                            "raw_id": "x-0031-r2"})
        self.assertEqual(a, b)

    def test_retitle_ignored(self):
        """blogroll retitles in place; the permalink is what identifies it."""
        a = feeds.identity({"source": "b", "link": "https://b.example/p",
                            "title": "Notes"})
        b = feeds.identity({"source": "b", "link": "https://b.example/p",
                            "title": "Notes (updated)"})
        self.assertEqual(a, b)

    def test_fragment_and_trailing_slash_normalised(self):
        base = feeds.identity({"source": "n", "link": "https://x.example/a"})
        self.assertEqual(base, feeds.identity({"source": "n", "link": "https://X.Example/a/"}))
        self.assertEqual(base, feeds.identity({"source": "n", "link": "https://x.example/a#top"}))

    def test_linkless_items_do_not_collide(self):
        """Items with no link must fall back to something, not all share ''."""
        a = feeds.identity({"source": "s", "link": "", "raw_id": "1", "title": "A"})
        b = feeds.identity({"source": "s", "link": "", "raw_id": "2", "title": "B"})
        self.assertNotEqual(a, b)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = store.connect(str(Path(self.tmp.name) / "t.sqlite3"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.db.close)
        # The `stdout` channel type prints each digest; keep it out of the way
        # so a real failure is readable.
        silence = contextlib.redirect_stdout(io.StringIO())
        silence.__enter__()
        self.addCleanup(silence.__exit__, None, None, None)

    def test_repeated_run_sends_nothing_new(self):
        """The actual reported bug: cron every 15 min, same items every time."""
        first = digest.run_once(config("snapshot-a"), self.db)
        self.assertGreater(first, 0)
        for _ in range(3):
            self.assertEqual(digest.run_once(config("snapshot-a"), self.db), 0)

    def test_churned_feed_sends_only_genuinely_new_item(self):
        """snapshot-b retitles, re-guids and re-stamps urls, but adds one story.

        Only newsroom 84130 ("Union responds...") is new. It matches `ops`
        (fee) and `everything`, so exactly 2 deliveries.
        """
        digest.run_once(config("snapshot-a"), self.db)
        self.assertEqual(digest.run_once(config("snapshot-b"), self.db), 2)
        self.assertEqual(digest.run_once(config("snapshot-b"), self.db), 0)

    def test_overlapping_channels_each_get_their_copy(self):
        """A global dedup would let the first channel starve the second."""
        chans = [
            {"name": "ops", "type": "stdout", "keywords": ["port"]},
            {"name": "everything", "type": "stdout", "keywords": []},
        ]
        digest.run_once(config("snapshot-a", chans), self.db)
        ops = store.count_sent(self.db, "ops")
        self.assertGreater(ops, 0)
        self.assertGreaterEqual(store.count_sent(self.db, "everything"), ops)

    def test_dry_run_does_not_consume_items(self):
        """A dry run must not make the next real run go quiet."""
        digest.run_once(config("snapshot-a"), self.db, dry_run=True)
        self.assertEqual(store.count_sent(self.db), 0)
        self.assertGreater(digest.run_once(config("snapshot-a"), self.db), 0)

    def test_failed_delivery_is_retried_not_dropped(self):
        """Losing an item is worse than repeating one, so don't mark on failure."""
        import channels

        chans = [{"name": "broken", "type": "stdout", "keywords": []}]
        original = channels.send

        def boom(chan_cfg, body):
            raise channels.DeliveryError("simulated outage")

        channels.send = boom
        try:
            self.assertEqual(digest.run_once(config("snapshot-a", chans), self.db), 0)
            self.assertEqual(store.count_sent(self.db, "broken"), 0)
        finally:
            channels.send = original
        # Recovered on the next tick: nothing was lost.
        self.assertGreater(digest.run_once(config("snapshot-a", chans), self.db), 0)

    def test_one_broken_channel_does_not_block_the_others(self):
        import channels

        chans = [
            {"name": "broken", "type": "webhook",
             "url": "http://127.0.0.1:9/nope", "keywords": []},
            {"name": "fine", "type": "stdout", "keywords": []},
        ]
        digest.run_once(config("snapshot-a", chans), self.db)
        self.assertEqual(store.count_sent(self.db, "broken"), 0)
        self.assertGreater(store.count_sent(self.db, "fine"), 0)

    def test_same_story_from_two_feeds_appears_once(self):
        cfg = config("snapshot-a")
        # Poll the same feed twice under different names, as if two providers
        # syndicated the same url.
        cfg["feeds"].append(dict(cfg["feeds"][0], name="newsroom-mirror"))
        cfg["channels"] = [{"name": "all", "type": "stdout", "keywords": []}]
        digest.run_once(cfg, self.db)
        sent = store.count_sent(self.db, "all")
        self.assertEqual(sent, len({i["identity"]
                                    for f in cfg["feeds"] for i in feeds.fetch(f)}))

    def test_archive_does_not_grow_per_tick(self):
        digest.run_once(config("snapshot-a"), self.db)
        after_one = store.count_items(self.db)
        for _ in range(3):
            digest.run_once(config("snapshot-a"), self.db)
        self.assertEqual(store.count_items(self.db), after_one)


class MigrationTests(unittest.TestCase):
    def test_legacy_db_is_deduped_and_keeps_earliest_first_seen(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "old.sqlite3")

        import sqlite3

        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
                raw_id TEXT, title TEXT NOT NULL, link TEXT NOT NULL,
                summary TEXT, published TEXT,
                first_seen TEXT NOT NULL DEFAULT (datetime('now')));
            INSERT INTO items (source,raw_id,title,link,first_seen) VALUES
             ('n','84121','Port fees','https://n.example/p?utm_campaign=w33','2026-08-14 09:15:00'),
             ('n','84121','Port fees','https://n.example/p?utm_campaign=w33','2026-08-14 09:30:00'),
             ('n','84121','Port fees','https://n.example/p?utm_campaign=w34','2026-08-14 09:45:00'),
             ('w','x-0031','Inquiry','https://w.example/i/1','2026-08-14 09:20:00'),
             ('w','x-0031-r2','Inquiry','https://w.example/i/1','2026-08-14 09:35:00');
            """
        )
        conn.commit()
        conn.close()

        db = store.connect(path)
        self.addCleanup(db.close)
        rows = {r["identity"]: r["first_seen"]
                for r in db.execute("SELECT identity, first_seen FROM items")}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows["https://n.example/p"], "2026-08-14 09:15:00")
        self.assertEqual(rows["https://w.example/i/1"], "2026-08-14 09:20:00")

        # Idempotent.
        store.connect(path).close()
        self.assertEqual(store.count_items(db), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
