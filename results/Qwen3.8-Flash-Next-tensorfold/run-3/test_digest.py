"""Checks for the cross-run dedup rules (see README, "Keeping digests fresh").

Run with:  python3 -m unittest test_digest -v

Everything runs offline against the fixtures/ snapshots. Snapshot b differs
from a exactly where it matters: one genuinely new newsroom story, one
retitled blogroll post and one edited wire item (both re-listed with new
ids/urls), plus the old items with rotated utm tags.
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

FEED_FORMATS = {"newsroom": "newsroom", "blogroll": "blogroll", "wire": "generic"}

CHANNELS = [
    {"name": "ops", "type": "stdout", "title": "Ops digest",
     "keywords": ["port", "levy", "fee"]},
    {"name": "energy", "type": "stdout", "title": "Energy digest",
     "keywords": ["tender", "grid", "offshore"]},
    {"name": "everything", "type": "stdout", "title": "Firehose", "keywords": []},
]


def config_for(snapshot):
    feeds = [
        {"name": name, "format": fmt, "url": f"file://{FIXTURES / snapshot / (name + '.json')}"}
        for name, fmt in FEED_FORMATS.items()
    ]
    return {"feeds": feeds, "channels": CHANNELS}


class DedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, snapshot, db, dry_run=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sent = digest.run_once(config_for(snapshot), db, dry_run=dry_run)
        return sent, out.getvalue()

    def test_second_identical_run_sends_nothing(self):
        with store.connect(self.db_path) as db:
            first, _ = self.run_once("snapshot-a", db)
            second, _ = self.run_once("snapshot-a", db)
        self.assertGreater(first, 0)
        self.assertEqual(second, 0)

    def test_edited_items_are_not_new_items(self):
        """Retitled post and edited wire item keep their identity (same
        link despite new titles/guids), so a re-listing sends only the
        genuinely new story."""
        with store.connect(self.db_path) as db:
            self.run_once("snapshot-a", db)
            sent, body = self.run_once("snapshot-b", db)
        self.assertEqual(sent, 2)  # the new story, once for ops, once for firehose
        self.assertEqual(body.count("Union responds to port fee inquiry"), 2)
        self.assertNotIn("Port fee inquiry opened", body)  # edited wire item
        self.assertNotIn("port fee arithmetic", body)      # retitled blogroll post

    def test_upgrade_seed_silences_the_old_spam(self):
        """A database carrying the pre-fix archive (everything fetched was
        also re-sent, to every matching channel) must not trigger one final
        re-send of the whole window on the first fixed run."""
        with store.connect(self.db_path) as db:
            for name, fmt in FEED_FORMATS.items():
                items = feeds.fetch({"name": name, "format": fmt,
                                     "url": f"file://{FIXTURES / 'snapshot-b' / (name + '.json')}"})
                store.record_items(db, name, items)  # stand-in for old record_items
            sent, body = self.run_once("snapshot-b", db)
        self.assertEqual(sent, 0)
        self.assertEqual(body, "")

    def test_dry_run_is_pure_and_read_only(self):
        with store.connect(self.db_path) as db:
            _, first = self.run_once("snapshot-a", db, dry_run=True)
            _, second = self.run_once("snapshot-a", db, dry_run=True)
            archived = db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
            marked = db.execute("SELECT COUNT(*) AS n FROM delivered").fetchone()["n"]
        self.assertIn("Port fee inquiry opened", first)
        self.assertEqual(first, second)  # preview must not consume "new" status
        self.assertEqual((archived, marked), (0, 0))


if __name__ == "__main__":
    unittest.main()
