#!/usr/bin/env python3
"""Tests for notify-digest.

Run with:
    python3 tests.py           # all tests
    python3 tests.py -v        # verbose
"""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import digest
import feeds
import store

HERE = Path(__file__).parent


# ---------------------------------------------------------------------------
# Unit tests: canonical link normalisation
# ---------------------------------------------------------------------------

class TestCanonicalLink(unittest.TestCase):
    """feeds._canonical_link() strips UTM params and leaves everything else."""

    def _cl(self, url):
        return feeds._canonical_link(url)

    def test_strips_utm_campaign(self):
        self.assertEqual(
            self._cl("https://newsroom.example/article?utm_campaign=w34"),
            "https://newsroom.example/article",
        )

    def test_strips_all_utm_params(self):
        url = (
            "https://example.com/p"
            "?utm_source=feed&utm_medium=email&utm_campaign=w34"
            "&utm_term=foo&utm_content=bar"
        )
        self.assertEqual(self._cl(url), "https://example.com/p")

    def test_preserves_non_utm_params(self):
        self.assertEqual(
            self._cl("https://example.com/p?page=2&utm_campaign=x"),
            "https://example.com/p?page=2",
        )

    def test_no_params_unchanged(self):
        url = "https://blog.example/port-fee-arithmetic"
        self.assertEqual(self._cl(url), url)

    def test_empty_string_unchanged(self):
        self.assertEqual(self._cl(""), "")

    def test_none_unchanged(self):
        self.assertIsNone(self._cl(None))

    def test_same_article_different_utm_campaign_normalises_identically(self):
        """The newsroom feed rotates utm_campaign weekly; both must map to the
        same canonical URL so the dedup layer treats them as the same item."""
        w33 = "https://newsroom.example/2026/08/port-fees?utm_source=feed&utm_campaign=w33"
        w34 = "https://newsroom.example/2026/08/port-fees?utm_source=feed&utm_campaign=w34"
        self.assertEqual(self._cl(w33), self._cl(w34))


# ---------------------------------------------------------------------------
# Unit tests: store layer
# ---------------------------------------------------------------------------

class TestSeenLinks(unittest.TestCase):

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.db = store.connect(self.db_path)

    def tearDown(self):
        self.db.close()
        os.unlink(self.db_path)

    def _item(self, link, source="test"):
        return {
            "title": "Title", "link": link, "summary": "",
            "published": "2026-01-01T00:00:00Z",
            "source": source, "raw_id": None,
        }

    def test_empty_db_returns_empty_set(self):
        self.assertEqual(store.seen_links(self.db), set())

    def test_stored_links_are_returned(self):
        items = [self._item("https://example.com/a"), self._item("https://example.com/b")]
        store.record_items(self.db, "test", items)
        self.assertEqual(
            store.seen_links(self.db),
            {"https://example.com/a", "https://example.com/b"},
        )

    def test_insert_or_ignore_silently_skips_duplicate(self):
        item = self._item("https://example.com/a")
        store.record_items(self.db, "test", [item])
        store.record_items(self.db, "test", [item])  # second call must not raise
        self.assertEqual(store.count_items(self.db), 1)


# ---------------------------------------------------------------------------
# Integration tests: end-to-end deduplication through run_once()
# ---------------------------------------------------------------------------

class TestDeduplication(unittest.TestCase):
    """Simulates cron ticks against the fixture snapshots."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.db = store.connect(self.db_path)

    def tearDown(self):
        self.db.close()
        os.unlink(self.db_path)

    def _cfg(self, snapshot):
        """Config dict pointing at one of the fixture snapshots."""
        base = HERE / "fixtures" / snapshot
        return {
            "feeds": [
                {"name": "newsroom", "format": "newsroom",
                 "url": str(base / "newsroom.json")},
                {"name": "blogroll", "format": "blogroll",
                 "url": str(base / "blogroll.json")},
                {"name": "wire",     "format": "generic",
                 "url": str(base / "wire.json")},
            ],
            # Single catch-all channel so we can count every item.
            "channels": [
                {"name": "all", "type": "stdout", "title": "All", "keywords": []},
            ],
        }

    def _run(self, snapshot):
        """Run one cron tick against *snapshot*, suppressing stdout."""
        with patch("sys.stdout", new_callable=io.StringIO):
            return digest.run_once(self._cfg(snapshot), self.db, dry_run=True)

    def test_first_run_sends_all_items(self):
        """snapshot-a has 6 items (2 newsroom + 2 blogroll + 2 wire); all new."""
        self.assertEqual(self._run("snapshot-a"), 6)

    def test_identical_second_run_sends_nothing(self):
        """Running the exact same snapshot twice must not re-deliver anything."""
        self._run("snapshot-a")
        self.assertEqual(
            self._run("snapshot-a"), 0,
            "second run of identical snapshot must send 0 items",
        )

    def test_only_new_item_sent_after_snapshot_b(self):
        """After snapshot-a, running snapshot-b must deliver exactly 1 new item.

        snapshot-b contains:
          newsroom — entry 84130 is new; 84121 and 84118 are the same articles
                     as snapshot-a but with utm_campaign=w34 instead of w33.
                     After UTM-stripping their canonical links match what is
                     already stored, so they are skipped.
          blogroll — same 2 permalinks, just updated title/excerpt.  Skipped.
          wire     — same 2 links; one guid changed to -r2 but the link did
                     not.  Skipped.
        """
        self._run("snapshot-a")
        self.assertEqual(
            self._run("snapshot-b"), 1,
            "only entry 84130 (Union responds…) is genuinely new in snapshot-b",
        )

    def test_utm_variant_not_treated_as_new(self):
        """A URL that only differs in UTM parameters must resolve to the same
        canonical link and therefore not be re-sent."""
        self._run("snapshot-a")
        seen = store.seen_links(self.db)
        # The canonical (UTM-stripped) newsroom URL must be in the seen set.
        self.assertIn("https://newsroom.example/2026/08/port-fees", seen)
        self.assertIn("https://newsroom.example/2026/08/offshore-tender", seen)

    def test_db_contains_canonical_links(self):
        """Items stored in the DB must use canonical (UTM-stripped) links."""
        self._run("snapshot-a")
        seen = store.seen_links(self.db)
        for link in seen:
            self.assertNotIn("utm_", link, f"link should be UTM-free: {link}")


if __name__ == "__main__":
    unittest.main()
