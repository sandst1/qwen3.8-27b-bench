"""Regression tests for the duplicate-send fix.

The fixtures in fixtures/snapshot-a and fixtures/snapshot-b are two
snapshots of the same three feeds taken ~4 hours apart. Snapshot B
re-serves almost everything from A, in three flavours of "same story,
new fingerprint":

* blogroll: no item ids at all; one story was retitled ("(updated)")
  under the same permalink;
* wire: the same story under a regenerated guid (…0031 → …0031-r2);
* newsroom: the same stories under URLs whose utm_campaign tag changed.

A run over B against a database that already processed A must therefore
send only the one genuinely new story (the union response), and only to
channels whose keywords it matches.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import digest
import channels
import store

FIXTURES = Path(__file__).parent / "fixtures"


def feed(snapshot, name, fmt):
    return {
        "name": name,
        "format": fmt,
        "url": str(FIXTURES / snapshot / f"{name}.json"),
    }


def config(snapshot, channels_):
    return {
        "feeds": [
            feed(snapshot, "newsroom", "newsroom"),
            feed(snapshot, "blogroll", "blogroll"),
            feed(snapshot, "wire", "generic"),
        ],
        "channels": channels_,
    }


def channel(name, keywords):
    return {"name": name, "type": "stdout", "title": name, "keywords": keywords}


EXAMPLE_CHANNELS = [
    channel("ops", ["port", "levy", "fee"]),
    channel("energy", ["tender", "grid", "offshore"]),
    channel("everything", []),
]


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "digest.sqlite3")
        self.db = store.connect(self.db_path)
        self.addCleanup(self.db.close)
        self.addCleanup(self._tmp.cleanup)
        # Deliver to a captured list instead of stdout, and keep output quiet.
        self.sent = []
        patch = mock.patch.object(
            digest.channels, "send",
            lambda chan_cfg, body: self.sent.append((chan_cfg["name"], body)),
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_repeat_run_of_same_snapshot_sends_nothing(self):
        cfg = config("snapshot-a", EXAMPLE_CHANNELS)
        first = digest.run_once(cfg, self.db)
        second = digest.run_once(cfg, self.db)
        self.assertGreater(first, 0)
        self.assertEqual(second, 0)

    def test_rehashed_duplicates_are_not_resent(self):
        """B re-serves A's stories with changed ids/URLs; none may re-send.

        Only the new union response is genuinely new, and it matches ops
        and everything only (no energy keywords). Anything else sent —
        e.g. the retitled blogroll post or the wire story whose guid was
        regenerated — is a duplicate reaching readers twice.
        """
        digest.run_once(config("snapshot-a", EXAMPLE_CHANNELS), self.db)
        self.sent.clear()
        sent = digest.run_once(config("snapshot-b", EXAMPLE_CHANNELS), self.db)
        self.assertEqual(sent, 2)
        bodies = " ".join(body for _, body in self.sent)
        self.assertNotIn("wire.example/i/0031", bodies)  # guid regenerated
        self.assertNotIn("wire.example/i/0918", bodies)
        self.assertIn("Union responds to port fee inquiry", bodies)

    def test_channel_that_missed_stories_still_gets_them(self):
        """The ledger is per audience: firehose never saw what ops saw."""
        digest.run_once(
            config("snapshot-a", [channel("ops", ["port", "levy", "fee"])]), self.db
        )
        self.sent.clear()
        sent = digest.run_once(
            config(
                "snapshot-a",
                [
                    channel("ops", ["port", "levy", "fee"]),
                    channel("everything", []),
                ],
            ),
            self.db,
        )
        self.assertEqual(sent, 6)  # snapshot A has six items, all new to firehose
        ops_bodies = [b for name, b in self.sent if name == "ops"]
        self.assertEqual(ops_bodies, [])

    def test_failed_delivery_retries_on_next_run(self):
        """Nothing is recorded when the send raises; cron ticks retry."""

        def boom(chan_cfg, body):
            raise channels.DeliveryError(f"{chan_cfg['name']}: down")

        cfg = config("snapshot-a", EXAMPLE_CHANNELS)
        with mock.patch.object(digest.channels, "send", boom):
            with self.assertRaises(channels.DeliveryError):
                digest.run_once(cfg, self.db)
        self.sent.clear()
        sent = digest.run_once(cfg, self.db)
        self.assertGreater(sent, 0)  # undelivered items were not burned

    def test_dry_run_leaves_ledger_untouched(self):
        """A dry run previews; it must not burn items for later runs."""
        cfg = config("snapshot-a", EXAMPLE_CHANNELS)
        with contextlib.redirect_stdout(io.StringIO()):
            digest.run_once(cfg, self.db, dry_run=True)
        self.sent.clear()
        sent = digest.run_once(cfg, self.db)
        self.assertGreater(sent, 0)


if __name__ == "__main__":
    unittest.main()
