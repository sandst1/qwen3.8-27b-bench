#!/usr/bin/env python3
"""notify-digest — poll a few feeds, filter them, send digests to channels.

Run from cron every 15 minutes. Feeds are polled in full each time (they
keep listing old entries, there's no "since" param we can rely on across
three different providers), so `run_once` is responsible for only sending
a channel the items it hasn't already received -- see the dedup_key check
below, and store.py's `deliveries` table. See README.md.
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
        if not matched:
            continue

        # We poll every 15 minutes but feeds keep listing old entries, so
        # most of `matched` has already gone out. Only the items whose
        # dedup_key hasn't been recorded for this channel before are
        # actually new; see store.py / feeds.py for what a dedup_key is.
        already = store.already_delivered(db, chan_cfg["name"], (i["dedup_key"] for i in matched))
        selected = [i for i in matched if i["dedup_key"] not in already]
        if not selected:
            continue

        body = render.digest(selected, chan_cfg)
        if dry_run:
            print(f"--- would send to {chan_cfg['name']} ---")
            print(body)
            continue
        channels.send(chan_cfg, body)
        # Only mark delivered *after* a successful send. If channels.send
        # raises, these items stay "undelivered" and will be retried next
        # tick, same as the existing best-effort delivery contract.
        store.mark_delivered(db, chan_cfg["name"], (i["dedup_key"] for i in selected))
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
