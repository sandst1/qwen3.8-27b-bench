#!/usr/bin/env python3
"""notify-digest — poll a few feeds, filter them, send digests to channels.

Run from cron every 15 minutes. See README.md.

Because this runs so often, most items are still sitting in a feed's
current window on the *next* poll too. `run_once` relies on
store.filter_unseen to only consider items it hasn't already recorded, and
only records items once they've actually been through a (non-dry-run)
digest pass -- see the comments in run_once for the two ways that can go
wrong if you touch this.
"""

import argparse
import sys
import tomllib
from pathlib import Path

import channels
import feeds
import render
import store


def load_config(path):
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def matches(item, filt):
    """A channel filter is a list of keywords; empty list means 'everything'."""
    keywords = filt.get("keywords", [])
    if not keywords:
        return True
    haystack = (item["title"] + " " + item.get("summary", "")).lower()
    return any(k.lower() in haystack for k in keywords)


def run_once(cfg, db, dry_run=False):
    all_items = []
    for feed_cfg in cfg["feeds"]:
        try:
            items = feeds.fetch(feed_cfg)
        except feeds.FeedError as exc:
            print(f"warn: feed {feed_cfg['name']} failed: {exc}", file=sys.stderr)
            continue
        all_items.extend(items)

    # Gate on "have we recorded this dedupe_key before?", not on anything
    # feed-shape-specific -- that logic lives in feeds.py. Without this,
    # the same item gets re-sent on every tick for as long as it stays in
    # the feed's current window (which, at a 15-minute poll interval, is
    # most of them, most of the time).
    new_items = store.filter_unseen(db, all_items)
    new_items.sort(key=lambda i: i.get("published", ""), reverse=True)

    sent = 0
    for chan_cfg in cfg["channels"]:
        selected = [i for i in new_items if matches(i, chan_cfg)]
        if not selected:
            continue
        body = render.digest(selected, chan_cfg)
        if dry_run:
            print(f"--- would send to {chan_cfg['name']} ---")
            print(body)
            continue
        channels.send(chan_cfg, body)
        sent += len(selected)

    # Recording is what makes an item stop showing up on future runs, so
    # it has to happen last, and it has to be skippable:
    #
    # - Skip entirely on --dry-run. Otherwise a preview run would "use up"
    #   items that a subsequent real run still needs to actually deliver.
    # - Skip on a channel-send exception (note this call is *after* the
    #   loop above, so an exception from channels.send skips it
    #   naturally). channels.send documents delivery as best-effort,
    #   relying on the next cron tick to retry a down channel; if we
    #   recorded items before/during that loop, a failed channel would
    #   have its items marked seen without ever having received them,
    #   which turns a transient outage into silent, permanent data loss.
    #   The trade-off: if channel 2 of 3 fails, channel 1 (which already
    #   succeeded) gets a duplicate on the retry. A duplicate is the bug
    #   we're fixing here, but only in the ordinary case; a dropped item
    #   during an outage is worse, so we accept the rare duplicate.
    if not dry_run:
        store.record_items(db, new_items)

    return sent


def main(argv=None):
    ap = argparse.ArgumentParser(prog="digest")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--db", default="digest.sqlite3")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be sent; does not mark items as seen",
    )
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    db = store.connect(args.db)
    try:
        sent = run_once(cfg, db, dry_run=args.dry_run)
        print(f"sent {sent} items")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
