"""Tests for notify-digest, runnable with `python3 -m unittest -v`.

The interesting cases are not "does dedup work" but the specific ways it can
be got wrong, so most of these are regression tests for a mistake that would
otherwise look correct in a casual run:

  * suppressing an item because a *different* item was sent (blogroll has no
    ids -- a naive key collapses the whole feed);
  * resending because a provider rotated something cosmetic (newsroom's utm
    parameters, wire's guid, blogroll's title);
  * losing an item because it was marked delivered before the send failed;
  * one channel's outage silencing the others.
"""

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest

import channels
import digest
import identity
import store

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def config(snapshot, channel_defs=None):
    """A config equivalent to config.example.toml, pointed at one snapshot."""
    if channel_defs is None:
        channel_defs = [
            {"name": "ops", "type": "stdout", "title": "Ops", "keywords": ["port", "levy", "fee"]},
            {"name": "energy", "type": "stdout", "title": "Energy", "keywords": ["tender", "grid", "offshore"]},
            {"name": "everything", "type": "stdout", "title": "Firehose", "keywords": []},
        ]
    return {
        "feeds": [
            {"name": n, "format": f, "url": f"file://{FIXTURES}/{snapshot}/{n}.json"}
            for n, f in (("newsroom", "newsroom"), ("blogroll", "blogroll"), ("wire", "generic"))
        ],
        "channels": channel_defs,
    }


class Harness(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(self.path)
        self.db = store.connect(self.path)
        self.addCleanup(self._teardown)

    def _teardown(self):
        self.db.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def tick(self, snapshot, **kw):
        """One cron run. Returns (sent, failures, captured_stdout).

        stderr is captured too (and exposed as self.stderr) so that expected
        warnings do not clutter the test report.
        """
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            sent, failures = digest.run_once(config(snapshot, kw.pop("channel_defs", None)), self.db, **kw)
        self.stderr = err.getvalue()
        return sent, failures, out.getvalue()


class TestSteadyState(Harness):
    def test_first_poll_sends_everything(self):
        sent, failures, out = self.tick("snapshot-a")
        # 6 items: ops matches 3, energy 3, firehose all 6.
        self.assertEqual(sent, 12)
        self.assertEqual(failures, 0)
        self.assertIn("Regulator opens inquiry into port fees", out)

    def test_unchanged_feed_sends_nothing(self):
        """The actual bug: cron every 15 minutes resent the whole feed."""
        self.tick("snapshot-a")
        for _ in range(5):
            sent, _, out = self.tick("snapshot-a")
            self.assertEqual(sent, 0)
            self.assertEqual(out, "")

    def test_only_the_genuinely_new_item_goes_out(self):
        """snapshot-a -> snapshot-b adds one item and edits three others."""
        self.tick("snapshot-a")
        sent, _, out = self.tick("snapshot-b")

        self.assertIn("Union responds to port fee inquiry", out)
        # ops + everything. Not energy: no tender/grid/offshore keyword.
        self.assertEqual(sent, 2)
        self.assertNotIn("Energy", out)

    def test_new_item_settles_after_one_poll(self):
        self.tick("snapshot-a")
        self.tick("snapshot-b")
        sent, _, out = self.tick("snapshot-b")
        self.assertEqual(sent, 0)
        self.assertEqual(out, "")

    def test_starting_from_snapshot_b_then_a_does_not_resend(self):
        """Feeds can shrink; a vanished item must not count as new later."""
        self.tick("snapshot-b")
        sent, _, _ = self.tick("snapshot-a")
        self.assertEqual(sent, 0)


class TestProviderChurn(Harness):
    """Each provider mutates items in a way that fools a different naive key."""

    def test_rotated_tracking_params_are_not_a_new_item(self):
        """newsroom moves utm_campaign=w33 -> w34 on unchanged items.

        Keying on the raw link resends both newsroom items here.
        """
        self.tick("snapshot-a")
        _, _, out = self.tick("snapshot-b")
        self.assertNotIn("Regulator opens inquiry into port fees", out)
        self.assertNotIn("Grid operator delays offshore tender", out)

    def test_regenerated_guid_is_not_a_new_item(self):
        """wire bumps guid ...-0031 -> ...-0031-r2 for a copy edit.

        Keying on raw_id resends this.
        """
        self.tick("snapshot-a")
        _, _, out = self.tick("snapshot-b")
        self.assertNotIn("Port fee inquiry opened", out)

    def test_edited_title_and_body_are_not_a_new_item(self):
        """blogroll retitles a post at an unchanged permalink.

        Keying on a content hash resends this. This is also the documented
        decision that edits do not re-notify -- if that is ever revisited,
        this is the test that should change.
        """
        self.tick("snapshot-a")
        _, _, out = self.tick("snapshot-b")
        self.assertNotIn("Notes on port fee arithmetic (updated)", out)

    def test_id_less_feed_items_stay_distinct(self):
        """blogroll has no id field at all, so raw_id is None for every post.

        A key built from a missing id gives every post the same identity and
        silently suppresses the entire feed after the first one. Both posts
        must arrive.
        """
        _, _, out = self.tick("snapshot-a")
        self.assertIn("Notes on port fee arithmetic", out)
        self.assertIn("A short history of offshore tenders", out)


class TestPerChannel(Harness):
    def test_one_item_reaches_every_matching_channel(self):
        """A global seen-set would give the port fee story to whichever
        channel is listed first and nothing to the rest."""
        _, _, out = self.tick("snapshot-a")
        self.assertEqual(out.count("Regulator opens inquiry into port fees"), 2)  # ops + firehose

    def test_channel_added_later_gets_a_backlog_not_silence(self):
        """A global seen-set would leave a newly configured channel deaf."""
        self.tick("snapshot-a")
        late = [{"name": "late", "type": "stdout", "title": "Late", "keywords": ["port"]}]
        sent, _, out = self.tick("snapshot-a", channel_defs=late)
        self.assertEqual(sent, 3)
        self.assertIn("Late", out)

    def test_widening_a_channels_keywords_delivers_the_backlog(self):
        narrow = [{"name": "ops", "type": "stdout", "title": "Ops", "keywords": ["port"]}]
        self.tick("snapshot-a", channel_defs=narrow)
        widened = [{"name": "ops", "type": "stdout", "title": "Ops", "keywords": ["port", "tender"]}]
        _, _, out = self.tick("snapshot-a", channel_defs=widened)
        self.assertIn("Tender timetable under review", out)
        self.assertNotIn("Port fee inquiry opened", out)  # already had this one


class TestDeliveryFailure(Harness):
    def setUp(self):
        super().setUp()
        self.broken = set()  # channel names that refuse delivery right now
        real = channels.send

        def fake(chan_cfg, body):
            if chan_cfg["name"] in self.broken:
                raise channels.DeliveryError(f"{chan_cfg['name']}: boom")
            return real(chan_cfg, body)

        channels.send = fake
        self.addCleanup(setattr, channels, "send", real)

    def test_failed_send_is_retried_not_lost(self):
        """Marking delivered before sending would drop these permanently --
        a worse bug than the duplicates we set out to fix."""
        self.broken = {"ops"}
        _, failures, _ = self.tick("snapshot-a")
        self.assertEqual(failures, 1)
        self.assertIn("channel ops failed", self.stderr)

        self.broken = set()  # channel comes back
        sent, failures, out = self.tick("snapshot-a")
        self.assertEqual(failures, 0)
        self.assertEqual(sent, 3)  # the three ops items, intact
        self.assertIn("Regulator opens inquiry into port fees", out)

    def test_one_broken_channel_does_not_silence_the_others(self):
        """`ops` is configured first; it used to abort the run and take
        `energy` and the firehose down with it for the whole outage."""
        self.broken = {"ops"}
        sent, failures, out = self.tick("snapshot-a")
        self.assertEqual(failures, 1)
        self.assertIn("Energy", out)
        self.assertIn("Firehose", out)
        self.assertEqual(sent, 9)  # everything except ops's 3

    def test_a_successful_channel_is_not_resent_after_a_peer_fails(self):
        self.broken = {"ops"}
        self.tick("snapshot-a")
        self.broken = set()
        _, _, out = self.tick("snapshot-a")
        self.assertNotIn("Firehose", out)  # already delivered, stays quiet

    def test_a_persistently_broken_channel_retries_indefinitely(self):
        self.broken = {"ops"}
        for _ in range(4):
            _, failures, _ = self.tick("snapshot-a")
            self.assertEqual(failures, 1)
        self.broken = set()
        sent, _, _ = self.tick("snapshot-a")
        self.assertEqual(sent, 3)  # nothing was dropped along the way

    def test_failure_sets_a_nonzero_exit_code(self):
        self.broken = {"ops"}
        tmp = tempfile.mkdtemp()
        cfg = os.path.join(tmp, "config.toml")
        with open(cfg, "w") as fh:
            fh.write(
                f'[[feeds]]\nname="newsroom"\nformat="newsroom"\n'
                f'url="file://{FIXTURES}/snapshot-a/newsroom.json"\n'
                '[[channels]]\nname="ops"\ntype="stdout"\nkeywords=["port"]\n'
            )
        db = os.path.join(tmp, "d.sqlite3")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = digest.main(["--config", cfg, "--db", db])
        self.assertEqual(rc, 1)

    def test_success_sets_a_zero_exit_code(self):
        tmp = tempfile.mkdtemp()
        cfg = os.path.join(tmp, "config.toml")
        with open(cfg, "w") as fh:
            fh.write(
                f'[[feeds]]\nname="newsroom"\nformat="newsroom"\n'
                f'url="file://{FIXTURES}/snapshot-a/newsroom.json"\n'
                '[[channels]]\nname="ops"\ntype="stdout"\nkeywords=["port"]\n'
            )
        db = os.path.join(tmp, "d.sqlite3")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(digest.main(["--config", cfg, "--db", db]), 0)


class TestDryRunAndSeed(Harness):
    def test_dry_run_writes_nothing(self):
        self.tick("snapshot-a", dry_run=True)
        self.assertEqual(store.count_deliveries(self.db), 0)
        self.assertEqual(store.count_items(self.db), 0)

    def test_dry_run_is_repeatable(self):
        _, _, first = self.tick("snapshot-a", dry_run=True)
        _, _, second = self.tick("snapshot-a", dry_run=True)
        self.assertEqual(first, second)
        self.assertIn("would send", first)

    def test_dry_run_reflects_what_is_actually_pending(self):
        self.tick("snapshot-a")
        _, _, out = self.tick("snapshot-a", dry_run=True)
        self.assertEqual(out, "")

    def test_seed_adopts_the_feed_without_sending(self):
        _, _, out = self.tick("snapshot-a", seed=True)
        self.assertNotIn("Regulator opens", out)
        self.assertGreater(store.count_deliveries(self.db), 0)

        sent, _, out = self.tick("snapshot-a")
        self.assertEqual(sent, 0)

        # ...but genuinely new items still arrive afterwards.
        sent, _, out = self.tick("snapshot-b")
        self.assertEqual(sent, 2)
        self.assertIn("Union responds to port fee inquiry", out)

    def test_dry_run_and_seed_are_rejected_together(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            digest.main(["--dry-run", "--seed"])


class TestRetention(Harness):
    def test_entries_for_items_still_on_the_feed_are_never_pruned(self):
        """The trap this design avoids: pruning on delivery date would expire
        a long-lived item while it is still being published, resending it."""
        self.tick("snapshot-a")
        self.db.execute("UPDATE deliveries SET first_delivered = datetime('now', '-400 days')")
        self.db.commit()

        sent, _, _ = self.tick("snapshot-a", retention_days=1)
        self.assertEqual(sent, 0)
        self.assertGreater(store.count_deliveries(self.db), 0)

    def test_entries_are_dropped_once_the_item_is_long_gone(self):
        self.tick("snapshot-a")
        self.db.execute("UPDATE deliveries SET last_seen = datetime('now', '-400 days')")
        self.db.commit()
        store.prune(self.db, 90)
        self.assertEqual(store.count_deliveries(self.db), 0)

    def test_retention_can_be_disabled(self):
        self.tick("snapshot-a")
        self.db.execute("UPDATE deliveries SET last_seen = datetime('now', '-4000 days')")
        self.db.commit()
        self.assertEqual(store.prune(self.db, 0), 0)


class TestIdentity(unittest.TestCase):
    def test_tracking_params_are_stripped(self):
        a = identity.normalize_url("https://n.example/a?utm_source=feed&utm_campaign=w33")
        b = identity.normalize_url("https://n.example/a?utm_source=feed&utm_campaign=w34")
        self.assertEqual(a, b)

    def test_meaningful_params_are_kept(self):
        a = identity.normalize_url("https://n.example/a?id=1")
        b = identity.normalize_url("https://n.example/a?id=2")
        self.assertNotEqual(a, b)

    def test_param_order_does_not_matter(self):
        a = identity.normalize_url("https://n.example/a?x=1&y=2")
        b = identity.normalize_url("https://n.example/a?y=2&x=1")
        self.assertEqual(a, b)

    def test_scheme_host_case_and_trailing_slash_are_ignored(self):
        base = identity.normalize_url("https://n.example/a")
        for variant in ("http://n.example/a", "https://N.Example/a", "https://n.example/a/"):
            self.assertEqual(identity.normalize_url(variant), base, variant)

    def test_fragments_are_significant(self):
        """Some feeds publish several items as anchors into one page;
        merging them would drop items."""
        a = identity.normalize_url("https://n.example/a#one")
        b = identity.normalize_url("https://n.example/a#two")
        self.assertNotEqual(a, b)

    def test_different_paths_stay_distinct(self):
        a = identity.normalize_url("https://newsroom.example/2026/08/port-fees?utm_campaign=w34")
        b = identity.normalize_url("https://newsroom.example/2026/08/port-fees-union?utm_campaign=w34")
        self.assertNotEqual(a, b)

    def test_ids_are_namespaced_by_source(self):
        a = identity.keys_for({"link": "", "raw_id": "7", "source": "newsroom"})
        b = identity.keys_for({"link": "", "raw_id": "7", "source": "wire"})
        self.assertFalse(set(a) & set(b))

    def test_missing_id_produces_no_id_key(self):
        keys = identity.keys_for({"link": "https://b.example/p", "raw_id": None, "source": "blogroll"})
        self.assertEqual(keys, ["url:b.example/p"])

    def test_an_item_always_has_at_least_one_key(self):
        """No key means it can never be recognised, so it sends forever."""
        keys = identity.keys_for({"link": "", "raw_id": None, "source": "x", "title": "t"})
        self.assertTrue(keys)

    def test_unparseable_links_are_used_verbatim_not_merged(self):
        a = identity.keys_for({"link": "not a url", "raw_id": None, "source": "x"})
        b = identity.keys_for({"link": "also not a url", "raw_id": None, "source": "x"})
        self.assertNotEqual(a, b)

    def test_aliases_accumulate_so_gradual_drift_is_tracked(self):
        """wire's guid changes one week and its URL the next. The first poll
        links the new guid to the known URL, so the second is still caught."""
        conn = store.connect(":memory:")
        v1 = {"link": "https://w.example/i/1", "raw_id": "g1", "source": "wire"}
        store.mark_delivered(conn, "ops", identity.keys_for(v1))

        v2 = {"link": "https://w.example/i/1", "raw_id": "g2", "source": "wire"}  # guid rotated
        self.assertTrue(store.already_delivered(conn, "ops", identity.keys_for(v2)))
        store.mark_delivered(conn, "ops", identity.keys_for(v2))

        v3 = {"link": "https://w.example/i/1-moved", "raw_id": "g2", "source": "wire"}  # url moved
        self.assertTrue(store.already_delivered(conn, "ops", identity.keys_for(v3)))


class TestMigration(unittest.TestCase):
    def test_a_pre_ledger_database_upgrades_in_place(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.addCleanup(os.unlink, path)

        old = sqlite3.connect(path)
        old.executescript(
            """
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, raw_id TEXT, title TEXT NOT NULL,
                link TEXT NOT NULL, summary TEXT, published TEXT,
                first_seen TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        old.execute(
            "INSERT INTO items (source, raw_id, title, link) VALUES ('wire','g1','Old','https://w/1')"
        )
        old.commit()
        old.close()

        conn = store.connect(path)
        self.assertEqual(store.count_items(conn), 1)  # archive preserved
        self.assertEqual(store.count_deliveries(conn), 0)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
        self.assertIn("ident", cols)
        conn.close()


class TestConfigValidation(Harness):
    def test_duplicate_channel_names_are_rejected(self):
        """Both would share a ledger, so the second silently goes quiet."""
        dupes = [
            {"name": "ops", "type": "stdout", "keywords": ["port"]},
            {"name": "ops", "type": "stdout", "keywords": ["tender"]},
        ]
        with self.assertRaises(digest.ConfigError) as ctx:
            self.tick("snapshot-a", channel_defs=dupes)
        self.assertIn("duplicate channel name", str(ctx.exception))

    def test_nameless_channel_is_rejected(self):
        with self.assertRaises(digest.ConfigError):
            self.tick("snapshot-a", channel_defs=[{"type": "stdout", "keywords": []}])

    def test_renaming_a_channel_resends_which_is_why_seed_exists(self):
        """Not an error, but surprising, so it is pinned and documented."""
        before = [{"name": "ops", "type": "stdout", "title": "Ops", "keywords": ["port"]}]
        self.tick("snapshot-a", channel_defs=before)

        after = [{"name": "operations", "type": "stdout", "title": "Ops", "keywords": ["port"]}]
        sent, _, _ = self.tick("snapshot-a", channel_defs=after)
        self.assertEqual(sent, 3)  # new name, no history

        # Seeding under the new name first is the documented way to avoid it.
        self.tick("snapshot-a", channel_defs=before)  # reset scenario
        renamed = [{"name": "ops2", "type": "stdout", "title": "Ops", "keywords": ["port"]}]
        self.tick("snapshot-a", channel_defs=renamed, seed=True)
        sent, _, _ = self.tick("snapshot-a", channel_defs=renamed)
        self.assertEqual(sent, 0)


    def test_bad_config_exits_2_without_touching_the_database(self):
        tmp = tempfile.mkdtemp()
        cfg = os.path.join(tmp, "config.toml")
        with open(cfg, "w") as fh:
            fh.write(
                f'[[feeds]]\nname="newsroom"\nformat="newsroom"\n'
                f'url="file://{FIXTURES}/snapshot-a/newsroom.json"\n'
                '[[channels]]\nname="ops"\ntype="stdout"\nkeywords=["port"]\n'
                '[[channels]]\nname="ops"\ntype="stdout"\nkeywords=["grid"]\n'
            )
        db = os.path.join(tmp, "d.sqlite3")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = digest.main(["--config", cfg, "--db", db])
        self.assertEqual(rc, 2)
        self.assertIn("duplicate channel name", err.getvalue())
        self.assertFalse(os.path.exists(db))  # bailed before connecting


if __name__ == "__main__":
    unittest.main()
