#!/usr/bin/env python3
"""notify-digest — poll a few feeds, filter them, send digests to channels.

Run from cron. See README.md.

Deduplication
-------------
Each cron run fetches every configured feed in full; providers don't offer
incremental/since-last-run endpoints.  Without deduplication every run
would re-deliver the same items to every channel — the original bug.

The fix lives in two places:

  store.filter_unsent(db, channel, items)
      Returns only items not yet recorded in the `sent` table for that
      channel.  Per-channel tracking means an item can legitimately appear
      in "ops" even after being sent to "energy".

  store.mark_sent(db, channel, items)
      Called after a successful delivery (or after a dry-run preview) so
      the next run skips those items.

See store.py for why `link` is used as the stable identity key across all
three feed formats (newsroom, blogroll, wire/generic).
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
        store.record_items(db, feed_cfg["name"], items)
        all_items.extend(items)

    all_items.sort(key=lambda i: i.get("published", ""), reverse=True)

    sent = 0
    for chan_cfg in cfg["channels"]:
        channel = chan_cfg["name"]

        # Only items matching this channel's keyword filter AND not already
        # sent to this channel in a previous run.
        selected = [i for i in all_items if matches(i, chan_cfg)]
        selected = store.filter_unsent(db, channel, selected)

        if not selected:
            continue

        body = render.digest(selected, chan_cfg)
        if dry_run:
            print(f"--- would send to {channel} ---")
            print(body)
            # Record as sent even in dry-run so repeated --dry-run invocations
            # also show only new items, matching production behaviour.
            store.mark_sent(db, channel, selected)
            continue

        channels.send(chan_cfg, body)
        # Mark after a successful send.  If send() raises, we don't mark, so
        # the next run will retry — acceptable; better than silent data loss.
        store.mark_sent(db, channel, selected)
        sent += len(selected)

    return sent


def main(argv=None):
    ap = argparse.ArgumentParser(prog="digest")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--db", default="digest.sqlite3")
    ap.add_argument("--dry-run", action="store_true")
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
