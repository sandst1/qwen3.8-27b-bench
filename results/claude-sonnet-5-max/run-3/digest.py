#!/usr/bin/env python3
"""notify-digest — poll a few feeds, filter them, send digests to channels.

Run from cron every 15 minutes. See README.md.

Feeds are polled snapshots, not "since last time" queries, so the same
item is normally present in many consecutive polls. Each channel run
therefore does two filters in sequence: `matches()` decides whether an
item is *relevant* to a channel (keywords), and
`store.filter_unsent()` decides whether it has already been *delivered*
to that channel (see store.py / feeds.py for how identity is defined).
Skipping either step reintroduces repeat notifications.
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
        matched = [i for i in all_items if matches(i, chan_cfg)]
        selected = store.filter_unsent(db, chan_cfg["name"], matched)
        if not selected:
            continue
        body = render.digest(selected, chan_cfg)
        if dry_run:
            print(f"--- would send to {chan_cfg['name']} ---")
            print(body)
            continue
        channels.send(chan_cfg, body)
        # Only record delivery *after* a successful send. If send() raises
        # (channels.py lets delivery errors propagate), these items stay
        # "unsent" and will be retried on the next cron tick instead of
        # being silently marked as delivered.
        store.record_deliveries(db, chan_cfg["name"], selected)
        sent += len(selected)

    return sent


def main(argv=None):
    ap = argparse.ArgumentParser(prog="digest")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--db", default="digest.sqlite3")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be sent without sending it or marking it delivered "
        "(repeatable previews of the same not-yet-sent items)",
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
