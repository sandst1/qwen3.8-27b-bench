"""Tests for store.py deduplication and the full run_once loop.

Run with:  python -m pytest tests/

The fixtures/ directory provides two feed snapshots taken at different times.
snapshot-b introduces one genuinely new story (newsroom entry 84130) alongside
several items that are the same stories re-fetched with minor differences:

  * newsroom 84121 and 84118 reappear with a new UTM campaign tag in the link.
  * The wire item 0031 reappears with a regenerated guid (the provider
    regenerates guids on every edit) but the same link.
  * The blogroll's first post reappears with an updated title and excerpt but
    the same permalink.

After a first run against snapshot-a, a second run against snapshot-b should
dispatch exactly one item (the new newsroom entry), not re-dispatch the rest.
"""

import pytest
import digest
import store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(source="src", link="https://example.com/story", raw_id=None, title="T"):
    return {
        "source": source,
        "link": link,
        "raw_id": raw_id,
        "title": title,
        "summary": "",
        "published": "",
    }


def _db():
    return store.connect(":memory:")


def _feed_cfg(name, fmt, path):
    return {"name": name, "format": fmt, "url": path}


def _cfg(snapshot):
    """Config pointing at one of the fixture snapshots."""
    return {
        "feeds": [
            _feed_cfg("newsroom", "newsroom", f"fixtures/{snapshot}/newsroom.json"),
            _feed_cfg("blogroll", "blogroll", f"fixtures/{snapshot}/blogroll.json"),
            _feed_cfg("wire",     "generic",  f"fixtures/{snapshot}/wire.json"),
        ],
        "channels": [
            {"name": "all", "type": "stdout", "title": "All", "keywords": []},
        ],
    }


# ---------------------------------------------------------------------------
# Unit tests: _seen_key
# ---------------------------------------------------------------------------

class TestSeenKey:
    def test_strips_utm_query_params(self):
        """UTM campaign rotation must not make an old story look new."""
        a = _item(link="https://newsroom.example/story?utm_campaign=w33")
        b = _item(link="https://newsroom.example/story?utm_campaign=w34")
        assert store._seen_key(a) == store._seen_key(b)

    def test_strips_fragment(self):
        a = _item(link="https://example.com/story")
        b = _item(link="https://example.com/story#section-2")
        assert store._seen_key(a) == store._seen_key(b)

    def test_different_paths_differ(self):
        a = _item(link="https://example.com/story-one")
        b = _item(link="https://example.com/story-two")
        assert store._seen_key(a) != store._seen_key(b)

    def test_different_sources_differ(self):
        """Same URL from two feeds must be treated as separate items."""
        a = _item(source="newsroom", link="https://example.com/story")
        b = _item(source="wire",     link="https://example.com/story")
        assert store._seen_key(a) != store._seen_key(b)


# ---------------------------------------------------------------------------
# Unit tests: filter_unseen / mark_seen
# ---------------------------------------------------------------------------

class TestFilterUnseen:
    def test_returns_all_when_nothing_seen(self):
        db = _db()
        items = [_item(link=f"https://example.com/{n}") for n in range(3)]
        assert store.filter_unseen(db, items) == items

    def test_excludes_already_seen(self):
        db = _db()
        old = _item(link="https://example.com/old")
        new = _item(link="https://example.com/new")
        store.mark_seen(db, [old])
        assert store.filter_unseen(db, [old, new]) == [new]

    def test_utm_rotation_not_re_sent(self):
        """Core regression: same story with a new UTM tag must be suppressed."""
        db = _db()
        first_fetch  = _item(link="https://newsroom.example/story?utm_campaign=w33")
        second_fetch = _item(link="https://newsroom.example/story?utm_campaign=w34")
        store.mark_seen(db, [first_fetch])
        assert store.filter_unseen(db, [second_fetch]) == []

    def test_wire_guid_change_not_re_sent(self):
        """Wire items whose guid regenerated on edit must be suppressed."""
        db = _db()
        original = _item(source="wire", link="https://wire.example/i/0031",
                         raw_id="wire-2026-08-14-0031")
        edited   = _item(source="wire", link="https://wire.example/i/0031",
                         raw_id="wire-2026-08-14-0031-r2")
        store.mark_seen(db, [original])
        assert store.filter_unseen(db, [edited]) == []

    def test_empty_input(self):
        assert store.filter_unseen(_db(), []) == []


class TestMarkSeen:
    def test_idempotent(self):
        """Calling mark_seen twice for the same item must not raise."""
        db = _db()
        i = _item()
        store.mark_seen(db, [i])
        store.mark_seen(db, [i])
        assert store.filter_unseen(db, [i]) == []

    def test_empty_is_noop(self):
        store.mark_seen(_db(), [])  # must not raise


# ---------------------------------------------------------------------------
# Integration tests: run_once with fixture snapshots
# ---------------------------------------------------------------------------

class TestRunOnce:
    def test_first_run_sends_all_items(self):
        """On a fresh database every fetched item is new and gets dispatched."""
        db = _db()
        sent = digest.run_once(_cfg("snapshot-a"), db)
        # 2 newsroom + 2 blogroll + 2 wire = 6 items, all through the single
        # "everything" channel, so sent == 6.
        assert sent == 6

    def test_second_run_sends_nothing(self):
        """Repeating the same snapshot must dispatch zero items."""
        db = _db()
        digest.run_once(_cfg("snapshot-a"), db)
        assert digest.run_once(_cfg("snapshot-a"), db) == 0

    def test_snapshot_b_sends_only_new_item(self):
        """
        snapshot-b introduces one new story (newsroom entry 84130) and
        re-delivers the remaining items with cosmetic changes:
          - newsroom 84121/84118: new UTM campaign tag in the link
          - wire 0031: guid regenerated (raw_id changes), link unchanged
          - blogroll post 1: title and excerpt updated, permalink unchanged

        Only the genuinely new story must be dispatched; the rest must be
        suppressed by deduplication.
        """
        db = _db()
        digest.run_once(_cfg("snapshot-a"), db)
        sent = digest.run_once(_cfg("snapshot-b"), db)
        assert sent == 1
