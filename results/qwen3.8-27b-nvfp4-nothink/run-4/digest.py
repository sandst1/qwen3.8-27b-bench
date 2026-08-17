#!/usr/bin/env python3
"""notify-digest — poll a few feeds, filter them, send digests to channels.

Run from cron. See README.md.
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


def run_once(cfg, db, dry_run=False, dedupe=True):
    # When dedupe is on, only items not yet in the `sent` table are
    # delivered; everything is still archived. With --all, dedupe is
    # bypassed and every fetched item is sent again.
    fresh_items = []
    for feed_cfg in cfg["feeds"]:
        try:
            items = feeds.fetch(feed_cfg)
        except feeds.FeedError as exc:
            print(f"warn: feed {feed_cfg['name']} failed: {exc}", file=sys.stderr)
            continue
        store.record_items(db, feed_cfg["name"], items)
        if dedupe:
            items = store.unseen_items(db, feed_cfg["name"], items)
        fresh_items.extend(items)

    fresh_items.sort(key=lambda i: i.get("published", ""), reverse=True)

    sent = 0
    for chan_cfg in cfg["channels"]:
        selected = [i for i in fresh_items if matches(i, chan_cfg)]
        if not selected:
            continue
        body = render.digest(selected, chan_cfg)
        if dry_run:
            print(f"--- would send to {chan_cfg['name']} ---")
            print(body)
            continue
        # Mark as sent only after the channel accepted the digest, so a
        # failed delivery is retried on the next tick.
        channels.send(chan_cfg, body)
        if dedupe:
            # Key by feed name, not channel name: an item is "sent" once
            # it has been delivered, period. The channel is irrelevant to
            # dedup (and one item can match several channels at once).
            for i in selected:
                store.mark_sent(db, i["source"], {store.item_key(i)})
        sent += len(selected)

    return sent


def main(argv=None):
    ap = argparse.ArgumentParser(prog="digest")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--db", default="digest.sqlite3")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--all",
        action="store_true",
        help="ignore the sent table and send every item (re-sends duplicates)",
    )
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    db = store.connect(args.db)
    try:
        sent = run_once(cfg, db, dry_run=args.dry_run, dedupe=not args.all)
        print(f"sent {sent} items")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
