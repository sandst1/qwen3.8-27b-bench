"""Tests for the deduplication fix.

Before this fix, run_once() sent every item returned by every feed on
every cron tick — the store was write-only and never consulted.  The fix
adds store.seen_links() and filters all_items to only unseen entries before
the send loop.

See store.py docstring for why `link` (URL) is the dedup key rather than
`raw_id` / guid.
"""

import unittest.mock as mock

import digest
import store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(title, link, raw_id=None, published="2026-08-01", source="test-feed"):
    return {
        "title": title,
        "link": link,
        "raw_id": raw_id,
        "summary": "",
        "published": published,
        "source": source,
    }


MINIMAL_CFG = {
    "feeds": [{"name": "test-feed", "url": "unused", "format": "generic"}],
    "channels": [{"name": "test-chan", "type": "stdout"}],
}

ITEM_A = _item("Old News",      "http://example.com/old", raw_id="guid-old")
ITEM_B = _item("Breaking News", "http://example.com/new", raw_id="guid-new",
               published="2026-08-02")


# ---------------------------------------------------------------------------
# store.seen_links unit tests
# ---------------------------------------------------------------------------

def test_seen_links_empty_db():
    conn = store.connect(":memory:")
    assert store.seen_links(conn, "any-source") == set()


def test_seen_links_returns_recorded_links():
    conn = store.connect(":memory:")
    items = [_item("A", "http://example.com/a"), _item("B", "http://example.com/b")]
    store.record_items(conn, "feed-x", items)
    assert store.seen_links(conn, "feed-x") == {
        "http://example.com/a",
        "http://example.com/b",
    }


def test_seen_links_scoped_by_source():
    """Links recorded for one source must not bleed into another."""
    conn = store.connect(":memory:")
    store.record_items(conn, "feed-x", [_item("A", "http://example.com/a")])
    assert store.seen_links(conn, "feed-y") == set()


# ---------------------------------------------------------------------------
# run_once deduplication integration tests
# ---------------------------------------------------------------------------

def test_first_run_sends_all_items():
    """On a fresh DB every fetched item should be dispatched."""
    conn = store.connect(":memory:")
    with mock.patch("feeds.fetch", return_value=[ITEM_A, ITEM_B]), \
         mock.patch("channels.send") as mock_send:
        sent = digest.run_once(MINIMAL_CFG, conn)

    assert sent == 2
    mock_send.assert_called_once()


def test_second_run_sends_nothing_when_feed_unchanged():
    """Items already dispatched must not be sent again."""
    conn = store.connect(":memory:")

    with mock.patch("feeds.fetch", return_value=[ITEM_A, ITEM_B]), \
         mock.patch("channels.send"):
        digest.run_once(MINIMAL_CFG, conn)

    # Second run, same feed contents.
    with mock.patch("feeds.fetch", return_value=[ITEM_A, ITEM_B]), \
         mock.patch("channels.send") as mock_send:
        sent = digest.run_once(MINIMAL_CFG, conn)

    assert sent == 0
    mock_send.assert_not_called()


def test_only_new_item_sent_on_subsequent_run():
    """When the feed gains one new item, only that item is dispatched."""
    conn = store.connect(":memory:")

    with mock.patch("feeds.fetch", return_value=[ITEM_A]), \
         mock.patch("channels.send"):
        digest.run_once(MINIMAL_CFG, conn)

    with mock.patch("feeds.fetch", return_value=[ITEM_A, ITEM_B]), \
         mock.patch("channels.send") as mock_send:
        sent = digest.run_once(MINIMAL_CFG, conn)

    assert sent == 1
    body = mock_send.call_args[0][1]   # second positional arg to channels.send
    assert ITEM_B["title"] in body
    assert ITEM_A["title"] not in body


def test_wire_guid_change_does_not_resend():
    """
    The wire (generic) feed regenerates its guid on edits — a new guid for
    an existing URL must not cause a re-send.  This would be a false positive
    if we deduped on raw_id instead of link.
    """
    conn = store.connect(":memory:")

    item_v1 = _item("Port fee inquiry opened", "http://example.com/port-fee",
                    raw_id="wire-0031")
    with mock.patch("feeds.fetch", return_value=[item_v1]), \
         mock.patch("channels.send"):
        digest.run_once(MINIMAL_CFG, conn)

    # Same URL, different guid (provider edited the item).
    item_v2 = {**item_v1, "raw_id": "wire-0031-r2", "summary": "corrected typo"}
    with mock.patch("feeds.fetch", return_value=[item_v2]), \
         mock.patch("channels.send") as mock_send:
        sent = digest.run_once(MINIMAL_CFG, conn)

    assert sent == 0
    mock_send.assert_not_called()


def test_blogroll_no_raw_id_deduplicates_by_link():
    """
    The blogroll feed never provides a raw_id (always None).  Dedup must
    still work via link.
    """
    conn = store.connect(":memory:")

    post = _item("My blog post", "http://blog.example.com/post-1", raw_id=None)
    with mock.patch("feeds.fetch", return_value=[post]), \
         mock.patch("channels.send"):
        digest.run_once(MINIMAL_CFG, conn)

    with mock.patch("feeds.fetch", return_value=[post]), \
         mock.patch("channels.send") as mock_send:
        sent = digest.run_once(MINIMAL_CFG, conn)

    assert sent == 0
    mock_send.assert_not_called()
