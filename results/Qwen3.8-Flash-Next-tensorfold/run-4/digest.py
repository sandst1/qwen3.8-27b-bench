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

    # Feeds mostly repeat their current contents on every poll, so each
    # channel only gets items it has never been sent before, tracked per
    # channel via the delivery ledger (see the store module docstring for
    # what counts as "the same item"). Items are marked after a successful
    # send only: a failed send aborts the run, and the next cron tick
    # retries what never got through.
    sent = 0
    for chan_cfg in cfg["channels"]:
        name = chan_cfg["name"]
        selected = [
            i
            for i in all_items
            if matches(i, chan_cfg) and not store.was_delivered(db, name, i)
        ]
        if not selected:
            continue
        body = render.digest(selected, chan_cfg)
        if dry_run:
            print(f"--- would send to {name} ---")
            print(body)
            continue
        channels.send(chan_cfg, body)
        store.mark_delivered(db, name, selected)
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
