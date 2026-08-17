"""Tests for the deduplication fix (store.seen_before / run_once).

The three feed formats expose three distinct failure modes that have to be
handled correctly:

  newsroom  — entry_id (raw_id) is stable, but the URL changes every week
              because UTM campaign tags rotate (utm_campaign=w33 → w34).
              Dedup must use raw_id, NOT link.

  wire      — the provider regenerates the guid whenever an item is edited,
              but the story link stays the same.
              Dedup must use link, NOT raw_id.

  blogroll  — no identifier at all; only the permalink is stable.
              Dedup must use link.

The fixtures directory has two snapshots (a and b) that exercise all three
scenarios.  snapshot-b has:
  • newsroom entry_id 84130  — genuinely new item              → must be sent
  • newsroom entry_id 84121  — same story, UTM tag changed     → must NOT be resent
  • newsroom entry_id 84118  — unchanged                       → must NOT be resent
  • wire link /i/0031        — guid regenerated (-r2 suffix)   → must NOT be resent
  • wire link /i/0918        — unchanged                       → must NOT be resent
  • blogroll /port-fee-arithmetic  — title+excerpt updated     → must NOT be resent
  • blogroll /offshore-tender-history — unchanged              → must NOT be resent

Run with:  python3 -m pytest test_dedup.py -v
       or: python3 test_dedup.py
"""

import io
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import digest
import feeds
import store

SNAP_A = Path(__file__).parent / "fixtures" / "snapshot-a"
SNAP_B = Path(__file__).parent / "fixtures" / "snapshot-b"


def _feed_cfg(name, fmt, snapshot):
    return {"name": name, "format": fmt, "url": f"file://{snapshot / (name + '.json')}"}


def make_db():
    """Return an in-memory SQLite connection with the schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store.SCHEMA)
    conn.commit()
    return conn


def load_snapshot(db, snapshot):
    """Load all three feeds from *snapshot* into the archive."""
    for name, fmt in [("newsroom", "newsroom"), ("blogroll", "blogroll"), ("wire", "generic")]:
        items = feeds.fetch(_feed_cfg(name, fmt, snapshot))
        store.record_items(db, name, items)


# ---------------------------------------------------------------------------
# Unit tests for store.seen_before()
# ---------------------------------------------------------------------------

class TestSeenBefore(unittest.TestCase):

    def setUp(self):
        self.db = make_db()
        load_snapshot(self.db, SNAP_A)

    # -- newsroom ------------------------------------------------------------

    def test_newsroom_seen_same_raw_id_different_utm(self):
        """entry_id 84121 reappears with a different UTM URL; seen via raw_id."""
        items = feeds.fetch(_feed_cfg("newsroom", "newsroom", SNAP_B))
        by_id = {i["raw_id"]: i for i in items}
        self.assertTrue(
            store.seen_before(self.db, by_id["84121"]),
            "entry_id 84121 should be seen (raw_id match, UTM tag changed)",
        )
        self.assertTrue(
            store.seen_before(self.db, by_id["84118"]),
            "entry_id 84118 should be seen (unchanged)",
        )

    def test_newsroom_new_item_not_seen(self):
        """entry_id 84130 is brand-new; must not be considered seen."""
        items = feeds.fetch(_feed_cfg("newsroom", "newsroom", SNAP_B))
        by_id = {i["raw_id"]: i for i in items}
        self.assertFalse(
            store.seen_before(self.db, by_id["84130"]),
            "entry_id 84130 is new and must not be seen",
        )

    # -- wire (generic) ------------------------------------------------------

    def test_wire_seen_same_link_different_guid(self):
        """Wire item 0031 reappears with guid suffix '-r2'; seen via link."""
        items = feeds.fetch(_feed_cfg("wire", "generic", SNAP_B))
        by_link = {i["link"]: i for i in items}
        self.assertTrue(
            store.seen_before(self.db, by_link["https://wire.example/i/0031"]),
            "wire/0031 should be seen (link unchanged, guid regenerated to -r2)",
        )
        self.assertTrue(
            store.seen_before(self.db, by_link["https://wire.example/i/0918"]),
            "wire/0918 should be seen (unchanged)",
        )

    # -- blogroll ------------------------------------------------------------

    def test_blogroll_seen_same_permalink_updated_content(self):
        """Blogroll item reappears with updated title+excerpt; seen via link."""
        items = feeds.fetch(_feed_cfg("blogroll", "blogroll", SNAP_B))
        by_link = {i["link"]: i for i in items}
        self.assertTrue(
            store.seen_before(self.db, by_link["https://blog.example/port-fee-arithmetic"]),
            "blogroll item should be seen (permalink unchanged, content updated)",
        )

    # -- edge cases ----------------------------------------------------------

    def test_novel_item_not_seen(self):
        """An item with a link and raw_id that have never been recorded is new."""
        novel = {
            "title": "Completely new story",
            "link": "https://example.com/brand-new",
            "summary": "",
            "published": "2026-08-15T10:00:00Z",
            "source": "newsroom",
            "raw_id": "99999",
        }
        self.assertFalse(store.seen_before(self.db, novel))

    def test_empty_db_nothing_seen(self):
        """On first run the archive is empty — no item should be considered seen."""
        fresh_db = make_db()
        items = feeds.fetch(_feed_cfg("newsroom", "newsroom", SNAP_A))
        for item in items:
            self.assertFalse(
                store.seen_before(fresh_db, item),
                f"fresh DB: '{item['title']}' should not be seen",
            )

    def test_null_raw_id_does_not_cross_match(self):
        """A blogroll item (raw_id=None) must not match a newsroom row that has
        raw_id=None in a hypothetical scenario — matching is scoped to source."""
        db = make_db()
        blogroll_item = {
            "title": "Some post",
            "link": "https://blog.example/some-post",
            "summary": "",
            "published": "",
            "source": "blogroll",
            "raw_id": None,
        }
        store.record_items(db, "blogroll", [blogroll_item])

        other_source_item = {
            "title": "Other post",
            "link": "https://other.example/other-post",
            "summary": "",
            "published": "",
            "source": "other",
            "raw_id": None,
        }
        self.assertFalse(
            store.seen_before(db, other_source_item),
            "different source with raw_id=None must not be considered seen",
        )


# ---------------------------------------------------------------------------
# Integration test: run_once() across two snapshots
# ---------------------------------------------------------------------------

class TestRunOnceDeduplicated(unittest.TestCase):
    """Drive run_once() twice — snapshot-a then snapshot-b — and verify that
    only the one genuinely new item (newsroom entry_id 84130) is sent on the
    second run."""

    def _make_cfg(self, snapshot):
        return {
            "feeds": [
                {"name": "newsroom", "format": "newsroom", "url": f"file://{snapshot / 'newsroom.json'}"},
                {"name": "blogroll", "format": "blogroll", "url": f"file://{snapshot / 'blogroll.json'}"},
                {"name": "wire",     "format": "generic",  "url": f"file://{snapshot / 'wire.json'}"},
            ],
            "channels": [
                # Firehose: receives everything so we can count without keyword noise.
                {"name": "everything", "type": "stdout", "title": "Test firehose", "keywords": []},
            ],
        }

    def test_first_run_sends_all_items(self):
        db = make_db()
        with patch("sys.stdout", io.StringIO()):
            sent = digest.run_once(self._make_cfg(SNAP_A), db)
        self.assertEqual(sent, 6, f"first run should send all 6 snapshot-a items; got {sent}")

    def test_second_run_sends_only_new_items(self):
        db = make_db()
        with patch("sys.stdout", io.StringIO()):
            digest.run_once(self._make_cfg(SNAP_A), db)
            sent = digest.run_once(self._make_cfg(SNAP_B), db)
        # snapshot-b introduces exactly one new item: newsroom entry_id 84130.
        self.assertEqual(sent, 1, f"second run should send only 1 new item; got {sent}")

    def test_archive_grows_by_one_on_second_run(self):
        """The items table should have 6 rows after the first run and 7 after
        the second — a direct measure that deduplication is writing correctly."""
        db = make_db()
        with patch("sys.stdout", io.StringIO()):
            digest.run_once(self._make_cfg(SNAP_A), db)
        self.assertEqual(store.count_items(db), 6)
        with patch("sys.stdout", io.StringIO()):
            digest.run_once(self._make_cfg(SNAP_B), db)
        self.assertEqual(store.count_items(db), 7)

    def test_third_run_sends_nothing(self):
        """After both snapshots have been processed, a repeat of snapshot-b
        must produce zero new items."""
        db = make_db()
        with patch("sys.stdout", io.StringIO()):
            digest.run_once(self._make_cfg(SNAP_A), db)
            digest.run_once(self._make_cfg(SNAP_B), db)
            sent = digest.run_once(self._make_cfg(SNAP_B), db)
        self.assertEqual(sent, 0, f"third run should send nothing; got {sent}")


if __name__ == "__main__":
    unittest.main()
