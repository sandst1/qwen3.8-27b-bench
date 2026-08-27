#!/usr/bin/env python3
"""notify-digest — poll a few feeds, filter them, send digests to channels.

Each run only sends items a channel has not already received. What counts as
"already sent" is tracked per channel in the SQLite ledger (store.py) using
the item's dedup_key (feeds.item_key). See feeds.py for why that key is the
canonical link and not the provider's id.

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


def select_for_channel(all_items, chan_cfg, already_sent):
    """Items this channel should receive now: it matches the filter, we have
    not already delivered them to *this* channel, and they appear once even if
    several feeds carried the same story in this run."""
    selected = []
    fresh = set()
    for item in all_items:
        key = item["dedup_key"]
        if key in already_sent or key in fresh:
            continue
        if matches(item, chan_cfg):
            selected.append(item)
            fresh.add(key)
    return selected


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
        already_sent = store.delivered_keys(db, chan_cfg["name"])
        selected = select_for_channel(all_items, chan_cfg, already_sent)
        if not selected:
            continue
        body = render.digest(selected, chan_cfg)
        if dry_run:
            print(f"--- would send to {chan_cfg['name']} ---")
            print(body)
            continue
        channels.send(chan_cfg, body)
        # Record only after a successful send: if delivery raised, nothing is
        # written for this channel and the items are retried on the next tick.
        store.record_deliveries(db, chan_cfg["name"], [i["dedup_key"] for i in selected])
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
