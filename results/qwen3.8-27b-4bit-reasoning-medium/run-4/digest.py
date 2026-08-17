#!/usr/bin/env python3
"""notify-digest — poll a few feeds, filter them, send digests to channels.

Run from cron. See README.md.

State lives in the SQLite db (`--db`, default `digest.sqlite3`). It remembers
which items each channel has already been sent, so a run only delivers what is
new to each channel — that is what stops the same items being re-sent every
tick. Keep the db persistent across runs; do not delete it or point cron at a
fresh one.
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
        selected = [i for i in all_items if matches(i, chan_cfg)]
        if not selected:
            continue
        # We run from cron every few minutes and feeds keep re-serving old
        # items, so only deliver what is new *to this channel*. Dedup is per
        # channel because each channel has its own filter: an item can be old
        # to one channel and new to another. See store.py for why we key on
        # (source, canonical link) rather than raw_id or the raw link.
        already = store.sent_keys(db, chan_cfg["name"])
        fresh = [i for i in selected if store.identity(i) not in already]
        if not fresh:
            continue
        body = render.digest(fresh, chan_cfg)
        if dry_run:
            print(f"--- would send to {chan_cfg['name']} ---")
            print(body)
            continue
        channels.send(chan_cfg, body)
        # Record the send only after the channel accepts it (at-least-once).
        store.mark_sent(db, chan_cfg["name"], fresh)
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
