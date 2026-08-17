#!/usr/bin/env python3
"""notify-digest — poll a few feeds, filter them, send digests to channels.

Run from cron. See README.md.

Each item is delivered to a given channel at most once: delivered
(channel, item) pairs are remembered in the `sent` table (see store.py), so
a feed that keeps listing the same story does not get re-sent every 15
minutes. Items are identified by canonical link — see store.item_key for
why that is the key none of the providers breaks.
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
        # Dedupe per channel: skip anything this channel already received.
        # Per channel (not global) because one item can match several
        # channels and each audience should get it exactly once.
        already = store.sent_keys(db, chan_cfg["name"])
        selected = [
            i
            for i in all_items
            if matches(i, chan_cfg) and store.item_key(i) not in already
        ]
        if not selected:
            continue
        body = render.digest(selected, chan_cfg)
        if dry_run:
            print(f"--- would send to {chan_cfg['name']} ---")
            print(body)
            continue
        channels.send(chan_cfg, body)
        # Record only after a successful send, so a down channel is retried
        # on the next tick instead of silently dropping those items.
        store.mark_sent(db, chan_cfg["name"], [store.item_key(i) for i in selected])
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
