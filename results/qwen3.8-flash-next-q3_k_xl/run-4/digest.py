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


def run_once(cfg, db, dry_run=False):
    """One cron tick: fetch, archive, then send each channel only the items
    it has not already received (see store.deliveries / feeds.item_key).

    Deliveries are recorded after each successful send. If a send fails the
    exception propagates (channels.py), nothing is recorded for that channel
    and the next tick retries it; channels that already succeeded will not
    re-send.
    """
    fetched = []
    seen = set()  # guard against a feed listing the same item twice
    for feed_cfg in cfg["feeds"]:
        try:
            items = feeds.fetch(feed_cfg)
        except feeds.FeedError as exc:
            print(f"warn: feed {feed_cfg['name']} failed: {exc}", file=sys.stderr)
            continue
        store.record_items(db, feed_cfg["name"], items)
        for item in items:
            key = feeds.item_key(item)
            if key not in seen:
                seen.add(key)
                fetched.append((key, item))

    fetched.sort(key=lambda ki: ki[1].get("published", ""), reverse=True)

    sent = 0
    for chan_cfg in cfg["channels"]:
        delivered = store.delivered_keys(db, chan_cfg["name"])
        selected = [
            (key, item)
            for key, item in fetched
            if key not in delivered and matches(item, chan_cfg)
        ]
        if not selected:
            continue
        body = render.digest([item for _, item in selected], chan_cfg)
        if dry_run:
            print(f"--- would send to {chan_cfg['name']} ---")
            print(body)
            continue
        channels.send(chan_cfg, body)
        store.record_deliveries(db, chan_cfg["name"], [key for key, _ in selected])
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
