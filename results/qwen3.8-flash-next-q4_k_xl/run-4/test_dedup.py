#!/usr/bin/env python3
"""Regression tests for duplicate-suppression. No third-party deps.

    python3 test_dedup.py

Uses the two feed snapshots in fixtures/ (same feeds, two points in time) to
prove that a story is delivered once per channel and never again, even when a
provider edits it, regenerates its id, or rewrites its tracking URL.
"""

import copy
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import digest
import store

FIXTURES = Path(__file__).parent / "fixtures"

CHANNELS = [
    {"name": "ops", "type": "stdout", "title": "Ops", "keywords": ["port", "levy", "fee"]},
    {"name": "energy", "type": "stdout", "title": "Energy", "keywords": ["tender", "grid", "offshore"]},
    {"name": "everything", "type": "stdout", "title": "Firehose", "keywords": []},
]


def config(snapshot):
    fmt = {"newsroom": "newsroom", "blogroll": "blogroll", "wire": "generic"}
    return {
        "feeds": [
            {"name": n, "format": fmt[n], "url": f"file://{FIXTURES / snapshot / (n + '.json')}"}
            for n in ("newsroom", "blogroll", "wire")
        ],
        "channels": copy.deepcopy(CHANNELS),
    }


def run(db, snapshot):
    """Run one cycle silently and return the number of items delivered."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        return digest.run_once(config(snapshot), db)


class DedupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.db = store.connect(self.tmp.name)

    def tearDown(self):
        self.db.close()
        Path(self.tmp.name).unlink()

    def test_first_run_delivers_everything_once(self):
        self.assertEqual(run(self.db, "snapshot-a"), 12)

    def test_identical_second_run_delivers_nothing(self):
        run(self.db, "snapshot-a")
        self.assertEqual(run(self.db, "snapshot-a"), 0)

    def test_second_snapshot_only_sends_the_genuinely_new_story(self):
        run(self.db, "snapshot-a")
        # snapshot-b adds one story (the union response, matching ops +
        # firehose). Everything else is an unchanged or *edited* repeat:
        #   wire 0031 -> guid regenerated, same link
        #   blogroll port-fee -> title edited, same permalink
        #   newsroom 84121/84118 -> utm_campaign w33 -> w34, same story
        self.assertEqual(run(self.db, "snapshot-b"), 2)
        self.assertEqual(run(self.db, "snapshot-b"), 0)

    def test_edited_wire_item_is_not_resent_despite_new_guid(self):
        run(self.db, "snapshot-a")
        run(self.db, "snapshot-b")
        # The wire story reached ops in snapshot-a; its regenerated guid must
        # not make it look new again in snapshot-b.
        run(self.db, "snapshot-b")
        wire_new = [
            i for i in self._seen()
            if i["source"] == "wire"
        ]
        self.assertEqual(len(wire_new), 2)  # 0031 and 0918, not three

    def test_archive_holds_each_story_once(self):
        run(self.db, "snapshot-a")
        run(self.db, "snapshot-a")
        run(self.db, "snapshot-b")
        self.assertEqual(store.count_items(self.db), 7)  # 6 + the new union story

    def test_dedup_is_per_channel(self):
        # A story matching only `ops` must not consume the item for a channel
        # it does not match, and vice versa. The union story is ops-only among
        # the keyword channels.
        run(self.db, "snapshot-a")
        run(self.db, "snapshot-b")
        ops = store.delivered_keys(self.db, "ops")
        energy = store.delivered_keys(self.db, "energy")
        self.assertTrue(ops - energy)  # ops saw stories energy did not

    def _seen(self):
        return self.db.execute("SELECT source, dedup_key FROM items").fetchall()


class MigrationTest(unittest.TestCase):
    def test_old_database_gains_dedup_key_without_losing_rows(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        tmp.close()
        path = tmp.name
        try:
            old = sqlite3.connect(path)
            old.executescript(
                "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " source TEXT NOT NULL, raw_id TEXT, title TEXT NOT NULL,"
                " link TEXT NOT NULL, summary TEXT, published TEXT,"
                " first_seen TEXT NOT NULL DEFAULT (datetime('now')));"
            )
            old.execute(
                "INSERT INTO items (source, title, link) VALUES ('wire','t','https://x/1')"
            )
            old.commit()
            old.close()

            db = store.connect(path)  # must not raise; must add the column
            cols = {r["name"] for r in db.execute("PRAGMA table_info(items)")}
            self.assertIn("dedup_key", cols)
            self.assertEqual(store.count_items(db), 1)
            db.close()
        finally:
            Path(path).unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
