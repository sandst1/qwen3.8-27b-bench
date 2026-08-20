#!/usr/bin/env python3
"""notify-digest — poll a few feeds, filter them, send digests to channels.

Run from cron. Each run sends a channel only the items it has not already
been sent; that state lives in the `sent` ledger in store.py, keyed by
`feeds.identity()`. See README.md.
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
        name = chan_cfg["name"]
        selected = [i for i in all_items if matches(i, chan_cfg)]

        # Drop anything this channel has already been given. Per channel, not
        # globally: channels overlap and each is entitled to its own copy.
        fresh = store.filter_unsent(db, name, selected)
        if not fresh:
            continue

        body = render.digest(fresh, chan_cfg)
        if dry_run:
            # Deliberately does not touch the ledger, so a dry run cannot
            # cause the next real run to skip items.
            print(f"--- would send to {name} ---")
            print(body)
            continue

        try:
            channels.send(chan_cfg, body)
        except channels.DeliveryError as exc:
            # Leave the ledger alone so these items are retried on the next
            # tick, and keep going: one broken webhook should not stop the
            # other channels from getting their digest.
            print(f"warn: channel {name} failed: {exc}", file=sys.stderr)
            continue

        # Record only after the channel accepted it. The reverse order would
        # turn any delivery failure into permanently dropped items, and a
        # missed item is worse than a repeated one.
        store.mark_sent(db, name, fresh)
        sent += len(fresh)

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
