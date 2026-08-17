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


def run_once(cfg, db, dry_run=False, mark_seen=False):
    """Poll every feed once and send each channel what it has not had yet.

    Feeds republish their whole window on every poll, so the same items come
    back four times an hour.  What makes a run idempotent is store.unsent():
    the digest for a channel contains only items with no delivery row for
    that channel, and rows are written after the send succeeds.

    A run therefore either sends an item or leaves it pending for the next
    tick; it never marks something it did not deliver.

    Returns the number of (channel, item) deliveries the run accounted for:
    sent for a normal run, would-be-sent for `dry_run`, adopted without
    sending for `mark_seen`.
    """
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
        matching = [i for i in all_items if matches(i, chan_cfg)]
        selected = store.unsent(db, name, matching)
        if not selected:
            continue

        if mark_seen:
            # Adopt the backlog without sending it: see main().
            store.mark_sent(db, name, selected)
            sent += len(selected)
            continue

        body = render.digest(selected, chan_cfg)
        if dry_run:
            # Deliberately no mark_sent: a dry run must be repeatable, and
            # must never suppress a real digest later.
            print(f"--- would send to {name} ---")
            print(body)
            sent += len(selected)
            continue

        try:
            channels.send(chan_cfg, body)
        except channels.DeliveryError as exc:
            # One channel being down should not hold up the others, and the
            # unmarked items are simply retried on the next tick.  This
            # mirrors how a failing feed is handled above.
            print(f"warn: channel {name} failed: {exc}", file=sys.stderr)
            continue

        store.mark_sent(db, name, selected)
        sent += len(selected)

    return sent


def main(argv=None):
    ap = argparse.ArgumentParser(prog="digest")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--db", default="digest.sqlite3")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--mark-seen",
        action="store_true",
        help="record everything currently in the feeds as already delivered,"
        " without sending anything; use when adding a channel that should"
        " start from now instead of receiving the backlog",
    )
    args = ap.parse_args(argv)

    if args.dry_run and args.mark_seen:
        ap.error("--dry-run and --mark-seen do the opposite things; pick one")

    cfg = load_config(args.config)
    db = store.connect(args.db)
    try:
        sent = run_once(cfg, db, dry_run=args.dry_run, mark_seen=args.mark_seen)
        if args.mark_seen:
            print(f"marked {sent} items as seen")
        elif args.dry_run:
            print(f"would send {sent} items")
        else:
            print(f"sent {sent} items")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
