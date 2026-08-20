"""Behavioural tests for the per-run dedup fix.

Run from the project root:  python3 -m unittest -v test_digest
"""

import os
import tempfile
import unittest

import channels
import digest
import store


def cfg_for(snapshot):
    base = f"fixtures/snapshot-{snapshot}"
    return {
        "feeds": [
            {"name": "newsroom", "format": "newsroom", "url": f"file://{base}/newsroom.json"},
            {"name": "blogroll", "format": "blogroll", "url": f"file://{base}/blogroll.json"},
            {"name": "wire", "format": "generic", "url": f"file://{base}/wire.json"},
        ],
        "channels": [
            {"name": "ops", "type": "stdout", "title": "Ops", "keywords": ["port", "levy", "fee"]},
            {"name": "energy", "type": "stdout", "title": "Energy", "keywords": ["tender", "grid", "offshore"]},
            {"name": "firehose", "type": "stdout", "title": "Firehose", "keywords": []},
        ],
    }


def titles_in(body):
    return [ln[2:].split("  [")[0] for ln in body.splitlines() if ln.startswith("* ")]


class DedupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.sqlite3")
        self.deliveries = []
        self._orig_send = channels.send
        channels.send = self._record

    def tearDown(self):
        channels.send = self._orig_send

    def _record(self, chan_cfg, body):
        self.deliveries.append((chan_cfg["name"], body))

    def fresh_db(self):
        store.connect(self.db_path).close()

    def test_second_run_sends_nothing(self):
        self.fresh_db()
        first = digest.run_once(cfg_for("a"), store.connect(self.db_path))
        second = digest.run_once(cfg_for("a"), store.connect(self.db_path))
        self.assertGreater(first, 0)
        self.assertEqual(second, 0, "the same items must not be sent twice")

    def test_firehose_is_not_starved(self):
        self.fresh_db()
        digest.run_once(cfg_for("a"), store.connect(self.db_path))
        firehose = [b for name, b in self.deliveries if name == "firehose"]
        self.assertEqual(len(firehose), 1)
        # Every item the firehose matches is delivered to it once.
        self.assertEqual(len(titles_in(firehose[0])), 6)

    def test_only_genuinely_new_items_are_sent(self):
        self.fresh_db()
        digest.run_once(cfg_for("a"), store.connect(self.db_path))
        self.deliveries.clear()
        total = digest.run_once(cfg_for("b"), store.connect(self.db_path))
        titles = [t for _, b in self.deliveries for t in titles_in(b)]
        # The brand-new item is delivered (to each channel that matches it)...
        self.assertIn("Union responds to port fee inquiry", titles)
        # ...but items that merely changed in place share a link and are not
        # re-sent, even though their title/guid text changed.
        self.assertEqual(set(titles), {"Union responds to port fee inquiry"})


if __name__ == "__main__":
    unittest.main()
