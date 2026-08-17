"""Tests for the dedup behaviour.

Run with: python3 -m unittest discover -v      (stdlib only, no deps)

The interesting cases are all "poll twice and see what happens the second
time", which is why fixtures/ ships two snapshots. snapshot-b is snapshot-a
after the providers have churned their fields:

    newsroom  same two entries, utm_campaign rotated w33 -> w34, plus one
              genuinely new entry (84130)
    blogroll  same two posts, one retitled "(updated)" with a new excerpt
    wire      same two items, one guid regenerated to "-r2" with a new body

Only entry 84130 is actually new. Everything else must stay quiet, and each
feed is a trap for a different naive dedup key -- see feeds.py.
"""

import tempfile
import unittest
from pathlib import Path

import channels
import digest
import feeds
import store

FIXTURES = Path(__file__).parent / "fixtures"


def config(snapshot, channel_defs=None):
    """A config pointed at one of the fixture snapshots."""
    return {
        "feeds": [
            {
                "name": "newsroom",
                "format": "newsroom",
                "url": f"file://{FIXTURES}/{snapshot}/newsroom.json",
            },
            {
                "name": "blogroll",
                "format": "blogroll",
                "url": f"file://{FIXTURES}/{snapshot}/blogroll.json",
            },
            {
                "name": "wire",
                "format": "generic",
                "url": f"file://{FIXTURES}/{snapshot}/wire.json",
            },
        ],
        "channels": channel_defs
        if channel_defs is not None
        else [{"name": "all", "type": "capture", "keywords": []}],
    }


class DigestTestCase(unittest.TestCase):
    """Base class: a temp DB and a capturing channel instead of real delivery."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = store.connect(str(Path(self._tmp.name) / "test.sqlite3"))
        self.addCleanup(self.db.close)

        # Captured deliveries: list of (channel_name, body).
        self.delivered = []
        # Channel names in here raise DeliveryError instead of delivering.
        self.broken = set()

        real_send = channels.send

        def fake_send(chan_cfg, body):
            name = chan_cfg["name"]
            if name in self.broken:
                raise channels.DeliveryError(f"{name}: pretend outage")
            self.delivered.append((name, body))

        channels.send = fake_send
        self.addCleanup(setattr, channels, "send", real_send)

    def run_tick(self, snapshot, channel_defs=None, **kwargs):
        """One cron tick. Returns the titles delivered during just this tick."""
        before = len(self.delivered)
        sent, failures = digest.run_once(
            config(snapshot, channel_defs), self.db, **kwargs
        )
        self.last_sent = sent
        self.last_failures = failures
        return self.delivered[before:]

    def titles(self, deliveries):
        """Item titles across a tick's deliveries, in order."""
        found = []
        for _name, body in deliveries:
            found.extend(
                line[2:].split("  [")[0]
                for line in body.splitlines()
                if line.startswith("* ")
            )
        return found


class TestRepeatSuppression(DigestTestCase):
    """The reported bug: same items over and over, every 15 minutes."""

    def test_first_tick_sends_everything(self):
        titles = self.titles(self.run_tick("snapshot-a"))
        self.assertEqual(len(titles), 6, "all six snapshot-a items should go out")

    def test_second_tick_of_unchanged_feeds_sends_nothing(self):
        self.run_tick("snapshot-a")
        self.assertEqual(self.run_tick("snapshot-a"), [])
        self.assertEqual(self.last_sent, 0)

    def test_stays_quiet_over_many_ticks(self):
        self.run_tick("snapshot-a")
        for _ in range(10):
            self.assertEqual(self.run_tick("snapshot-a"), [])


class TestProviderChurn(DigestTestCase):
    """Each feed breaks a different naive dedup key. None may re-notify."""

    def setUp(self):
        super().setUp()
        self.run_tick("snapshot-a")
        self.second = self.titles(self.run_tick("snapshot-b"))

    def test_only_the_genuinely_new_item_is_sent(self):
        self.assertEqual(self.second, ["Union responds to port fee inquiry"])

    def test_newsroom_utm_rotation_does_not_resend(self):
        # Both surviving newsroom entries have a different URL in snapshot-b
        # (utm_campaign w33 -> w34). Keying on the raw link would resend them.
        self.assertNotIn("Regulator opens inquiry into port fees", self.second)
        self.assertNotIn("Grid operator delays offshore tender", self.second)

    def test_blogroll_retitle_does_not_resend(self):
        # The post was edited in place: new title, new excerpt, same permalink.
        # Keying on a content hash would resend it.
        self.assertNotIn("Notes on port fee arithmetic (updated)", self.second)
        self.assertNotIn("Notes on port fee arithmetic", self.second)

    def test_wire_guid_regeneration_does_not_resend(self):
        # guid went "wire-2026-08-14-0031" -> "...-0031-r2" with an edited
        # body. Keying on raw_id/guid would resend it.
        self.assertNotIn("Port fee inquiry opened", self.second)

    def test_edits_are_recorded_in_the_archive_even_though_not_resent(self):
        # We stayed quiet, but the archive should still reflect the new text
        # so "what does this item say now?" remains answerable.
        row = self.db.execute(
            "SELECT title, summary FROM items WHERE source = 'blogroll'"
            " AND link = 'https://blog.example/port-fee-arithmetic'"
        ).fetchone()
        self.assertEqual(row["title"], "Notes on port fee arithmetic (updated)")
        self.assertIn("corrected table", row["summary"])


class TestChannelIndependence(DigestTestCase):
    """Overlapping channels must not consume each other's items."""

    CHANNELS = [
        {"name": "ops", "type": "capture", "keywords": ["port", "levy", "fee"]},
        {"name": "everything", "type": "capture", "keywords": []},
    ]

    def test_overlapping_channels_both_receive_the_same_item(self):
        deliveries = self.run_tick("snapshot-a", self.CHANNELS)
        got = {name for name, _ in deliveries}
        self.assertEqual(got, {"ops", "everything"})
        # A global "already sent" flag would have given it to ops only.
        for name in ("ops", "everything"):
            bodies = [b for n, b in deliveries if n == name]
            self.assertTrue(
                any("port fee" in b.lower() for b in bodies),
                f"{name} should have received the port fee item",
            )

    def test_channel_order_does_not_change_the_outcome(self):
        forward = self.titles(self.run_tick("snapshot-a", self.CHANNELS))
        self.setUp()  # fresh DB
        reversed_ = self.titles(
            self.run_tick("snapshot-a", list(reversed(self.CHANNELS)))
        )
        self.assertCountEqual(forward, reversed_)

    def test_a_new_channel_added_later_gets_a_backfill(self):
        self.run_tick("snapshot-a", [self.CHANNELS[0]])
        later = self.run_tick("snapshot-a", self.CHANNELS)
        self.assertEqual({n for n, _ in later}, {"everything"})


class TestDeliveryFailures(DigestTestCase):
    """Failed delivery must retry, not silently vanish."""

    CHANNELS = [
        {"name": "flaky", "type": "capture", "keywords": []},
        {"name": "healthy", "type": "capture", "keywords": []},
    ]

    def test_failed_delivery_is_retried_on_the_next_tick(self):
        self.broken = {"flaky"}
        self.run_tick("snapshot-a", self.CHANNELS)
        self.assertEqual(store.count_sent(self.db, "flaky"), 0)

        self.broken = set()
        retried = self.run_tick("snapshot-a", self.CHANNELS)
        self.assertEqual({n for n, _ in retried}, {"flaky"})
        self.assertEqual(len(self.titles(retried)), 6)

    def test_one_broken_channel_does_not_block_the_others(self):
        self.broken = {"flaky"}
        deliveries = self.run_tick("snapshot-a", self.CHANNELS)
        self.assertEqual({n for n, _ in deliveries}, {"healthy"})
        self.assertEqual(store.count_sent(self.db, "healthy"), 6)

    def test_failures_are_reported_to_the_caller(self):
        self.broken = {"flaky"}
        self.run_tick("snapshot-a", self.CHANNELS)
        self.assertEqual(self.last_failures, ["flaky"])

    def test_a_broken_feed_does_not_stop_the_run(self):
        cfg = config("snapshot-a")
        cfg["feeds"][0]["url"] = "file:///nonexistent/newsroom.json"
        sent, failures = digest.run_once(cfg, self.db)
        self.assertEqual(failures, ["newsroom"])
        self.assertEqual(sent, 4, "the other two feeds should still deliver")


class TestDryRun(DigestTestCase):
    """--dry-run must not perturb state, or testing config silences alerts."""

    def test_dry_run_writes_nothing(self):
        self.run_tick("snapshot-a", dry_run=True)
        self.assertEqual(store.count_items(self.db), 0)
        self.assertEqual(store.count_sent(self.db), 0)

    def test_dry_run_does_not_suppress_the_next_real_run(self):
        self.run_tick("snapshot-a", dry_run=True)
        self.assertEqual(len(self.titles(self.run_tick("snapshot-a"))), 6)


class TestMarkSeen(DigestTestCase):
    """--mark-seen: adopt the current feed window without notifying."""

    def test_mark_seen_delivers_nothing_but_suppresses_the_next_tick(self):
        self.assertEqual(self.run_tick("snapshot-a", mark_seen=True), [])
        self.assertEqual(store.count_sent(self.db, "all"), 6)
        self.assertEqual(self.run_tick("snapshot-a"), [])

    def test_items_arriving_after_mark_seen_are_still_delivered(self):
        self.run_tick("snapshot-a", mark_seen=True)
        titles = self.titles(self.run_tick("snapshot-b"))
        self.assertEqual(titles, ["Union responds to port fee inquiry"])


class TestArchive(DigestTestCase):
    """The archive should answer "did this ever come through?" without noise."""

    def test_repeated_polls_do_not_duplicate_archive_rows(self):
        for _ in range(5):
            self.run_tick("snapshot-a")
        self.assertEqual(store.count_items(self.db), 6)

    def test_last_seen_advances_while_first_seen_holds(self):
        self.run_tick("snapshot-a")
        row = self.db.execute(
            "SELECT first_seen, last_seen FROM items LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row["last_seen"])
        self.assertLessEqual(row["first_seen"], row["last_seen"])


class TestUrlNormalisation(unittest.TestCase):
    """Identity-level URL comparison. Delivered links are never rewritten."""

    def assertSameDoc(self, a, b):
        self.assertEqual(feeds.normalise_url(a), feeds.normalise_url(b))

    def assertDifferentDoc(self, a, b):
        self.assertNotEqual(feeds.normalise_url(a), feeds.normalise_url(b))

    def test_campaign_parameters_are_ignored(self):
        self.assertSameDoc(
            "https://n.example/a?utm_source=feed&utm_campaign=w33",
            "https://n.example/a?utm_source=feed&utm_campaign=w34",
        )
        self.assertSameDoc("https://n.example/a?fbclid=xyz", "https://n.example/a")

    def test_meaningful_parameters_are_kept(self):
        self.assertDifferentDoc("https://n.example/a?page=1", "https://n.example/a?page=2")
        self.assertDifferentDoc("https://n.example/a?id=7", "https://n.example/a")

    def test_parameter_order_does_not_matter(self):
        self.assertSameDoc("https://n.example/a?x=1&y=2", "https://n.example/a?y=2&x=1")

    def test_case_and_trailing_slash_and_fragment(self):
        self.assertSameDoc("HTTPS://N.Example/a", "https://n.example/a")
        self.assertSameDoc("https://n.example/a/", "https://n.example/a")
        self.assertSameDoc("https://n.example/a#section", "https://n.example/a")
        self.assertSameDoc("https://n.example:443/a", "https://n.example/a")

    def test_path_case_is_significant(self):
        # Paths are case-sensitive per RFC 3986; merging them would drop items.
        self.assertDifferentDoc("https://n.example/A", "https://n.example/a")

    def test_distinct_documents_stay_distinct(self):
        self.assertDifferentDoc(
            "https://newsroom.example/2026/08/port-fees?utm_campaign=w34",
            "https://newsroom.example/2026/08/port-fees-union?utm_campaign=w34",
        )

    def test_unparseable_input_is_not_collapsed(self):
        self.assertEqual(feeds.normalise_url(""), "")
        self.assertDifferentDoc("not a url", "also not a url")


class TestIdentityFallback(unittest.TestCase):
    """Items with no usable id and no link must not collapse onto one key."""

    def test_linkless_items_get_distinct_identities(self):
        a = feeds._identity("url", "", "src", "First headline", "2026-01-01")
        b = feeds._identity("url", "", "src", "Second headline", "2026-01-01")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("hash:"))

    def test_identical_linkless_items_share_an_identity(self):
        args = ("url", "", "src", "Same headline", "2026-01-01")
        self.assertEqual(feeds._identity(*args), feeds._identity(*args))


if __name__ == "__main__":
    unittest.main()
