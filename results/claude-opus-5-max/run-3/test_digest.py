#!/usr/bin/env python3
"""Tests for the de-duplication behaviour.

Run with:  python3 -m unittest -v

The interesting cases all come from the two fixture snapshots, which were
captured at different times and between them exercise every way a provider
can make the same item look new:

  * newsroom stamps a fresh ?utm_campaign on every URL each week
  * wire regenerates its guid ("-r2") when an item is edited
  * blogroll retitles a post to "... (updated)" and rewrites the excerpt

Only one item is genuinely new in snapshot-b: newsroom entry 84130,
"Union responds to port fee inquiry".
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import channels
import digest
import feeds
import identity
import store

ROOT = Path(__file__).resolve().parent


def config(snapshot, channel_defs=None):
    """A config pointing at one of the fixture snapshots."""
    return {
        "feeds": [
            {"name": "newsroom", "format": "newsroom",
             "url": f"file://{ROOT}/fixtures/{snapshot}/newsroom.json"},
            {"name": "blogroll", "format": "blogroll",
             "url": f"file://{ROOT}/fixtures/{snapshot}/blogroll.json"},
            {"name": "wire", "format": "generic",
             "url": f"file://{ROOT}/fixtures/{snapshot}/wire.json"},
        ],
        "channels": channel_defs if channel_defs is not None else [
            {"name": "ops", "type": "stdout", "title": "Ops",
             "keywords": ["port", "levy", "fee"]},
            {"name": "energy", "type": "stdout", "title": "Energy",
             "keywords": ["tender", "grid", "offshore"]},
            {"name": "everything", "type": "stdout", "title": "Firehose",
             "keywords": []},
        ],
    }


class DigestTestCase(unittest.TestCase):
    """Base case with a temp database and channel delivery captured."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = store.connect(str(Path(self._tmp.name) / "test.sqlite3"))
        self.addCleanup(self.db.close)

        self.delivered = []            # (channel name, digest body)
        self.failing = set()           # channels that raise on send

        real_send = channels.send

        def fake_send(chan_cfg, body):
            name = chan_cfg["name"]
            if name in self.failing:
                raise channels.DeliveryError(f"{name}: pretend outage")
            self.delivered.append((name, body))

        channels.send = fake_send
        self.addCleanup(setattr, channels, "send", real_send)

    def run_digest(self, snapshot="snapshot-a", **kwargs):
        return digest.run_once(config(snapshot), self.db, **kwargs)

    def titles_sent_to(self, channel):
        """Every item title delivered to `channel` across all runs."""
        return [
            line.split("  [")[0][2:]
            for name, body in self.delivered
            if name == channel
            for line in body.splitlines()
            if line.startswith("* ")
        ]


class TestRepeatSuppression(DigestTestCase):
    """The reported bug: cron re-sent everything every 15 minutes."""

    def test_first_run_sends_everything(self):
        self.assertEqual(self.run_digest(), 12)

    def test_unchanged_feed_sends_nothing_on_the_next_tick(self):
        self.run_digest()
        self.delivered.clear()
        self.assertEqual(self.run_digest(), 0)
        self.assertEqual(self.delivered, [])

    def test_stays_quiet_over_many_ticks(self):
        self.run_digest()
        self.delivered.clear()
        for _ in range(8):
            self.run_digest()
        self.assertEqual(self.delivered, [])

    def test_only_genuinely_new_items_go_out_after_a_feed_update(self):
        self.run_digest("snapshot-a")
        self.delivered.clear()

        # ops and everything match the new item; energy does not.
        self.assertEqual(self.run_digest("snapshot-b"), 2)
        self.assertEqual(
            self.titles_sent_to("ops"), ["Union responds to port fee inquiry"]
        )
        self.assertEqual(self.titles_sent_to("energy"), [])


class TestProviderQuirks(DigestTestCase):
    """Each provider breaks a different "obvious" identifier."""

    def setUp(self):
        super().setUp()
        self.run_digest("snapshot-a")
        self.delivered.clear()
        self.run_digest("snapshot-b")
        self.firehose = self.titles_sent_to("everything")

    def test_rotating_utm_campaign_is_not_a_new_item(self):
        # newsroom bumps ?utm_campaign=w33 -> w34 on every URL each week.
        self.assertNotIn("Regulator opens inquiry into port fees", self.firehose)
        self.assertNotIn("Grid operator delays offshore tender", self.firehose)

    def test_regenerated_guid_is_not_a_new_item(self):
        # wire rewrote guid wire-2026-08-14-0031 -> ...-0031-r2 on edit.
        self.assertNotIn("Port fee inquiry opened", self.firehose)

    def test_retitled_and_edited_post_is_not_a_new_item(self):
        # blogroll retitled the post and rewrote its excerpt.
        self.assertNotIn(
            "Notes on port fee arithmetic (updated)", self.firehose
        )

    def test_a_genuinely_new_item_still_arrives(self):
        self.assertEqual(self.firehose, ["Union responds to port fee inquiry"])


class TestChannelIndependence(DigestTestCase):
    """Delivery state is per channel, not global."""

    def test_overlapping_channels_both_receive_a_matching_item(self):
        self.run_digest()
        # "Port fee inquiry opened" matches ops and the firehose. A global
        # sent-flag would let whichever ran first swallow it.
        self.assertIn("Port fee inquiry opened", self.titles_sent_to("ops"))
        self.assertIn(
            "Port fee inquiry opened", self.titles_sent_to("everything")
        )

    def test_channel_order_does_not_change_what_is_delivered(self):
        self.run_digest()
        first = sorted(self.titles_sent_to("everything"))

        reversed_cfg = config("snapshot-a")
        reversed_cfg["channels"].reverse()
        self.delivered.clear()
        digest.run_once(reversed_cfg, self.db)
        self.assertEqual(self.delivered, [])  # nothing new either way

        self.assertEqual(len(first), 6)

    def test_channel_added_later_receives_the_backlog(self):
        self.run_digest()
        self.delivered.clear()

        cfg = config("snapshot-a", channel_defs=[
            {"name": "ops", "type": "stdout", "keywords": ["port"]},
            {"name": "newcomer", "type": "stdout", "keywords": []},
        ])
        digest.run_once(cfg, self.db)
        self.assertEqual(self.titles_sent_to("ops"), [])
        self.assertEqual(len(self.titles_sent_to("newcomer")), 6)

    def test_mark_seen_adopts_the_backlog_without_sending(self):
        cfg = config("snapshot-a", channel_defs=[
            {"name": "newcomer", "type": "stdout", "keywords": []},
        ])
        digest.run_once(cfg, self.db, mark_seen=True)
        self.assertEqual(self.delivered, [])

        digest.run_once(cfg, self.db)
        self.assertEqual(self.delivered, [])


class TestFailedDeliveryIsRetried(DigestTestCase):
    """Nothing is marked as sent unless it actually went out."""

    def test_failing_channel_retries_on_the_next_tick(self):
        self.failing = {"ops"}
        self.run_digest()
        self.assertEqual(self.titles_sent_to("ops"), [])

        self.failing = set()
        self.run_digest()
        self.assertEqual(len(self.titles_sent_to("ops")), 3)

    def test_one_failing_channel_does_not_block_the_others(self):
        self.failing = {"ops"}
        self.run_digest()
        # ops is first in the config; the rest must still be delivered.
        self.assertEqual(len(self.titles_sent_to("energy")), 3)
        self.assertEqual(len(self.titles_sent_to("everything")), 6)

    def test_healthy_channels_are_not_re_sent_while_one_recovers(self):
        self.failing = {"ops"}
        self.run_digest()
        self.delivered.clear()

        self.failing = set()
        self.run_digest()
        self.assertEqual(self.titles_sent_to("everything"), [])

    def test_failing_feed_does_not_mark_its_items(self):
        cfg = config("snapshot-a")
        cfg["feeds"][0]["url"] = f"file://{ROOT}/fixtures/nope.json"
        digest.run_once(cfg, self.db)
        self.assertEqual(len(self.titles_sent_to("everything")), 4)

        self.delivered.clear()
        self.run_digest("snapshot-a")
        self.assertEqual(len(self.titles_sent_to("everything")), 2)


class TestDryRun(DigestTestCase):
    """A dry run must never suppress a real digest."""

    def test_dry_run_delivers_nothing_and_marks_nothing(self):
        self.run_digest(dry_run=True)
        self.assertEqual(self.delivered, [])

        self.assertEqual(self.run_digest(), 12)

    def test_dry_run_is_repeatable(self):
        self.assertEqual(self.run_digest(dry_run=True), 12)
        self.assertEqual(self.run_digest(dry_run=True), 12)

    def test_dry_run_and_mark_seen_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            digest.main(["--dry-run", "--mark-seen", "--config", "x"])


class TestArchive(DigestTestCase):
    """`items` is an archive, so first_seen has to mean something."""

    def test_items_are_archived_once_not_once_per_run(self):
        self.run_digest()
        after_first = store.count_items(self.db)
        self.assertEqual(after_first, 6)

        for _ in range(4):
            self.run_digest()
        self.assertEqual(store.count_items(self.db), after_first)

    def test_new_items_are_still_archived(self):
        self.run_digest("snapshot-a")
        self.run_digest("snapshot-b")
        self.assertEqual(store.count_items(self.db), 7)


class TestItemIdentity(unittest.TestCase):
    """Unit level checks on the key itself."""

    def test_tracking_parameters_are_stripped(self):
        self.assertEqual(
            identity.canonical_link("https://e.example/a?utm_source=x&gclid=1"),
            "https://e.example/a",
        )

    def test_meaningful_parameters_are_kept(self):
        # Dropping these would merge unrelated items and silently suppress
        # them, which is worse than a duplicate.
        self.assertNotEqual(
            identity.canonical_link("https://e.example/p?id=1"),
            identity.canonical_link("https://e.example/p?id=2"),
        )

    def test_parameter_order_does_not_matter(self):
        self.assertEqual(
            identity.canonical_link("https://e.example/a?b=1&a=2"),
            identity.canonical_link("https://e.example/a?a=2&b=1"),
        )

    def test_host_case_and_trailing_slash_are_normalised(self):
        self.assertEqual(
            identity.canonical_link("https://E.example/a/"),
            identity.canonical_link("https://e.example/a"),
        )

    def test_anchor_is_dropped_but_hash_routing_is_kept(self):
        self.assertEqual(
            identity.canonical_link("https://e.example/a#section-2"),
            identity.canonical_link("https://e.example/a"),
        )
        self.assertNotEqual(
            identity.canonical_link("https://e.example/#/post/1"),
            identity.canonical_link("https://e.example/#/post/2"),
        )

    def test_falls_back_to_raw_id_when_there_is_no_link(self):
        key = identity.item_key({"link": "", "raw_id": "g1", "source": "wire"})
        self.assertEqual(key, "raw:wire:g1")

    def test_raw_id_fallback_is_scoped_per_feed(self):
        # Raw ids are only unique within a provider.
        self.assertNotEqual(
            identity.item_key({"link": "", "raw_id": "1", "source": "a"}),
            identity.item_key({"link": "", "raw_id": "1", "source": "b"}),
        )

    def test_items_with_neither_link_nor_id_do_not_collapse_together(self):
        # An empty key would merge them all and suppress the lot.
        first = identity.item_key({"source": "b", "title": "one"})
        second = identity.item_key({"source": "b", "title": "two"})
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("sha:"))

    def test_key_namespaces_cannot_collide(self):
        self.assertNotEqual(
            identity.item_key({"link": "x"}),
            identity.item_key({"raw_id": "x", "source": ""}),
        )


class TestStoreDetails(DigestTestCase):
    def test_same_story_from_two_feeds_is_sent_once(self):
        shared = "https://shared.example/story"
        items = [
            {"title": "A", "link": shared, "source": "one", "raw_id": "1"},
            {"title": "A", "link": shared + "?utm_source=z", "source": "two",
             "raw_id": "2"},
        ]
        self.assertEqual(len(store.unsent(self.db, "ops", items)), 1)

    def test_unsent_preserves_order(self):
        items = [
            {"title": t, "link": f"https://e.example/{t}", "source": "s",
             "raw_id": None}
            for t in ("c", "a", "b")
        ]
        got = [i["title"] for i in store.unsent(self.db, "ops", items)]
        self.assertEqual(got, ["c", "a", "b"])


class TestLegacyDatabaseMigration(unittest.TestCase):
    """Existing deployments have an items table and no deliveries table."""

    def test_pre_existing_database_is_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "legacy.sqlite3")

            legacy = sqlite3.connect(path)
            legacy.executescript(
                """
                CREATE TABLE items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL, raw_id TEXT,
                    title TEXT NOT NULL, link TEXT NOT NULL,
                    summary TEXT, published TEXT,
                    first_seen TEXT NOT NULL DEFAULT (datetime('now'))
                );
                """
            )
            # The duplicate rows the old code accumulated, four times an hour.
            for _ in range(3):
                legacy.execute(
                    "INSERT INTO items (source, raw_id, title, link)"
                    " VALUES ('wire', 'g1', 'Old', 'https://e.example/old')"
                )
            legacy.commit()
            legacy.close()

            conn = store.connect(path)
            self.addCleanup(conn.close)

            columns = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
            self.assertIn("item_key", columns)
            self.assertEqual(store.count_items(conn), 3)  # left alone

            # And the new state table works.
            items = [{"title": "New", "link": "https://e.example/new",
                      "source": "wire", "raw_id": "g2"}]
            self.assertEqual(len(store.unsent(conn, "ops", items)), 1)
            store.mark_sent(conn, "ops", items)
            self.assertEqual(store.unsent(conn, "ops", items), [])


if __name__ == "__main__":
    unittest.main()
