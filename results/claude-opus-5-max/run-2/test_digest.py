#!/usr/bin/env python3
"""Regression tests for digest de-duplication.

Run with:  python3 -m unittest -v

The bug these exist to prevent: the job ran from cron every 15 minutes and
re-sent every matching item on every tick, because it kept no record of what it
had already delivered and no stable notion of item identity.

The fixtures are two snapshots of the same three feeds taken at different
times, and each one mutates a different field between snapshots. That makes
them an exact reproduction of the bug, so most tests below replay
snapshot-a -> snapshot-b and assert on what a channel actually receives.
"""

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

import channels
import digest
import feeds
import store

FIXTURES = Path(__file__).parent / "fixtures"


def config(snapshot):
    """The example config, pointed at one of the fixture snapshots."""
    return {
        "feeds": [
            {"name": "newsroom", "format": "newsroom",
             "url": f"file://{FIXTURES}/{snapshot}/newsroom.json"},
            {"name": "blogroll", "format": "blogroll",
             "url": f"file://{FIXTURES}/{snapshot}/blogroll.json"},
            {"name": "wire", "format": "generic",
             "url": f"file://{FIXTURES}/{snapshot}/wire.json"},
        ],
        "channels": [
            {"name": "ops", "type": "capture", "title": "Ops digest",
             "keywords": ["port", "levy", "fee"]},
            {"name": "energy", "type": "capture", "title": "Energy digest",
             "keywords": ["tender", "grid", "offshore"]},
            {"name": "everything", "type": "capture", "title": "Firehose",
             "keywords": []},
        ],
    }


class DigestTestCase(unittest.TestCase):
    """Base class that captures deliveries instead of performing them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = store.connect(str(Path(tmp.name) / "test.sqlite3"))
        self.addCleanup(self.db.close)

        self.delivered = []          # [(channel_name, body), ...]
        self.fail_channels = set()   # channels whose delivery raises

        real_send = channels.send

        def fake_send(chan_cfg, body):
            name = chan_cfg["name"]
            if name in self.fail_channels:
                raise channels.DeliveryError(f"{name}: simulated outage")
            self.delivered.append((name, body))

        channels.send = fake_send
        self.addCleanup(setattr, channels, "send", real_send)

    def run_once(self, snapshot, **kwargs):
        self.delivered = []
        # dry-run prints the digests it would send; keep that out of the
        # test report.
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            sent = digest.run_once(config(snapshot), self.db, **kwargs)
        self.stdout = captured.getvalue()
        return sent

    def titles_for(self, channel):
        """Item titles delivered to a channel, across all runs since reset."""
        found = []
        for name, body in self.delivered:
            if name != channel:
                continue
            found += [
                line.split("  [")[0].removeprefix("* ")
                for line in body.splitlines()
                if line.startswith("* ")
            ]
        return found


class TestRepeatDelivery(DigestTestCase):
    """The reported bug: the same items arriving over and over."""

    def test_first_run_delivers_everything_matching(self):
        sent = self.run_once("snapshot-a")
        self.assertEqual(sent, 12)
        self.assertEqual(len(self.titles_for("ops")), 3)
        self.assertEqual(len(self.titles_for("energy")), 3)
        self.assertEqual(len(self.titles_for("everything")), 6)

    def test_identical_poll_delivers_nothing(self):
        self.run_once("snapshot-a")
        sent = self.run_once("snapshot-a")
        self.assertEqual(sent, 0, "unchanged feeds must produce no delivery")
        self.assertEqual(self.delivered, [])

    def test_repeated_polls_stay_quiet(self):
        self.run_once("snapshot-a")
        for _ in range(5):  # a bit over an hour of cron ticks
            self.assertEqual(self.run_once("snapshot-a"), 0)


class TestItemIdentity(DigestTestCase):
    """Each feed mutates a different field between the two snapshots.

    One test per failure mode, so a break points straight at the cause rather
    than at a single opaque count.
    """

    def test_only_the_genuinely_new_item_is_sent(self):
        self.run_once("snapshot-a")
        sent = self.run_once("snapshot-b")
        self.assertEqual(sent, 2, "one new article, matching two channels")
        self.assertEqual(
            self.titles_for("ops"), ["Union responds to port fee inquiry"]
        )
        self.assertEqual(self.titles_for("energy"), [])

    def test_rotating_utm_parameters_are_not_a_new_item(self):
        # newsroom keeps entry_id but rewrites utm_campaign=w33 -> w34.
        self.run_once("snapshot-a")
        self.run_once("snapshot-b")
        self.assertNotIn(
            "Regulator opens inquiry into port fees", self.titles_for("everything")
        )

    def test_regenerated_guid_is_not_a_new_item(self):
        # wire rewrites guid ...-0031 -> ...-0031-r2 for the same article.
        self.run_once("snapshot-a")
        self.run_once("snapshot-b")
        self.assertNotIn("Port fee inquiry opened", self.titles_for("everything"))

    def test_edited_title_and_summary_are_not_a_new_item(self):
        # blogroll edits the post in place; the permalink is unchanged.
        self.run_once("snapshot-a")
        self.run_once("snapshot-b")
        self.assertNotIn(
            "Notes on port fee arithmetic (updated)", self.titles_for("everything")
        )

    def test_blogroll_items_do_not_collapse_into_one(self):
        # Its raw_id is always None; keying on that would merge the whole feed.
        keys = {
            i["key"]
            for i in feeds.fetch(config("snapshot-a")["feeds"][1])
        }
        self.assertEqual(len(keys), 2)


class TestPerChannelLedger(DigestTestCase):
    """An item can match several channels; each must get its own copy once."""

    def test_item_reaches_every_matching_channel(self):
        self.run_once("snapshot-a")
        self.assertIn(
            "Regulator opens inquiry into port fees", self.titles_for("ops")
        )
        self.assertIn(
            "Regulator opens inquiry into port fees", self.titles_for("everything")
        )

    def test_a_new_channel_gets_the_backlog(self):
        self.run_once("snapshot-a")
        cfg = config("snapshot-a")
        cfg["channels"].append(
            {"name": "late", "type": "capture", "title": "Late", "keywords": ["port"]}
        )
        self.delivered = []
        digest.run_once(cfg, self.db)
        self.assertEqual(len(self.titles_for("late")), 3)
        self.assertEqual(self.titles_for("ops"), [], "existing channels stay quiet")


class TestDeliveryFailures(DigestTestCase):
    """The ledger is written after a successful send, so nothing is lost."""

    def test_failed_delivery_is_retried_next_tick(self):
        self.fail_channels = {"ops"}
        self.run_once("snapshot-a")
        self.assertEqual(self.titles_for("ops"), [])

        self.fail_channels = set()
        self.run_once("snapshot-a")
        self.assertEqual(
            len(self.titles_for("ops")), 3, "items must survive a channel outage"
        )

    def test_one_broken_channel_does_not_block_the_others(self):
        self.fail_channels = {"ops"}
        self.run_once("snapshot-a")
        self.assertEqual(len(self.titles_for("energy")), 3)
        self.assertEqual(len(self.titles_for("everything")), 6)

    def test_healthy_channels_are_not_resent_after_a_peer_fails(self):
        self.fail_channels = {"ops"}
        self.run_once("snapshot-a")
        self.fail_channels = set()
        self.run_once("snapshot-a")
        self.assertEqual(self.titles_for("energy"), [])


class TestDryRunAndSeed(DigestTestCase):
    def test_dry_run_does_not_consume_items(self):
        self.run_once("snapshot-a", dry_run=True)
        self.assertEqual(self.delivered, [], "dry run must not deliver")
        self.assertIn("would send to ops", self.stdout)
        self.assertEqual(
            self.run_once("snapshot-a"), 12, "dry run must not mark items sent"
        )

    def test_seed_marks_without_delivering(self):
        seeded = self.run_once("snapshot-a", seed=True)
        self.assertEqual(seeded, 12)
        self.assertEqual(self.delivered, [], "seeding must not deliver")
        self.assertEqual(self.run_once("snapshot-a"), 0)

    def test_seed_still_lets_later_items_through(self):
        self.run_once("snapshot-a", seed=True)
        self.assertEqual(self.run_once("snapshot-b"), 2)


class TestLinkNormalisation(unittest.TestCase):
    def test_strips_campaign_parameters_and_fragment(self):
        self.assertEqual(
            feeds.normalise_link("https://e.example/a?utm_source=feed&id=7#top"),
            "https://e.example/a?id=7",
        )

    def test_preserves_meaningful_query_parameters(self):
        # Over-stripping merges unrelated items, which is worse than a repeat.
        for url in ("https://e.example/p?id=1", "https://e.example/p?id=2"):
            self.assertEqual(feeds.normalise_link(url), url)

    def test_ignores_parameter_order_and_host_case(self):
        self.assertEqual(
            feeds.normalise_link("https://E.example/a?b=2&a=1"),
            feeds.normalise_link("https://e.example/a?a=1&b=2"),
        )

    def test_path_case_is_significant(self):
        self.assertNotEqual(
            feeds.normalise_link("https://e.example/A"),
            feeds.normalise_link("https://e.example/a"),
        )

    def test_keys_are_scoped_per_feed(self):
        one = feeds.fetch({"name": "one", "format": "generic",
                           "url": "file://" + str(FIXTURES / "snapshot-a/wire.json")})
        two = feeds.fetch({"name": "two", "format": "generic",
                           "url": "file://" + str(FIXTURES / "snapshot-a/wire.json")})
        self.assertTrue({i["key"] for i in one}.isdisjoint({i["key"] for i in two}))


class TestArchive(DigestTestCase):
    def test_archive_keeps_one_row_per_item(self):
        self.run_once("snapshot-a")
        self.run_once("snapshot-a")
        self.assertEqual(store.count_items(self.db), 6)

    def test_archive_preserves_original_first_seen(self):
        self.run_once("snapshot-a")
        before = self.db.execute(
            "SELECT key, first_seen FROM items ORDER BY key"
        ).fetchall()
        self.db.execute("UPDATE items SET first_seen = '2000-01-01 00:00:00'")
        self.db.commit()
        self.run_once("snapshot-b")
        after = dict(
            self.db.execute("SELECT key, first_seen FROM items").fetchall()
        )
        for row in before:
            self.assertEqual(after[row["key"]], "2000-01-01 00:00:00")


class TestMigration(unittest.TestCase):
    """A database created before this fix must keep working."""

    OLD_SCHEMA = """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL, raw_id TEXT, title TEXT NOT NULL,
        link TEXT NOT NULL, summary TEXT, published TEXT,
        first_seen TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """

    def test_pre_dedupe_database_is_upgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.sqlite3")
            old = sqlite3.connect(path)
            old.executescript(self.OLD_SCHEMA)
            old.execute(
                "INSERT INTO items (source, title, link) VALUES ('wire','Old','u')"
            )
            old.commit()
            old.close()

            conn = store.connect(path)
            self.addCleanup(conn.close)
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
            self.assertIn("key", columns)
            self.assertEqual(store.count_items(conn), 1, "history is preserved")
            self.assertEqual(store.count_deliveries(conn), 0)

            # And the upgraded database still de-duplicates.
            cfg = config("snapshot-a")
            self.assertEqual(digest.run_once(cfg, conn, seed=True), 12)
            self.assertEqual(digest.run_once(cfg, conn), 0)


if __name__ == "__main__":
    unittest.main()
