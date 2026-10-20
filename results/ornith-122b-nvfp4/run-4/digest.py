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
    # Collect this tick's items first; nothing is archived until delivery
    # succeeds (see below). Keep feeds as (source, items) so we can archive
    # them under the right source afterwards.
    fetched = []
    for feed_cfg in cfg["feeds"]:
        try:
            items = feeds.fetch(feed_cfg)
        except feeds.FeedError as exc:
            print(f"warn: feed {feed_cfg['name']} failed: {exc}", file=sys.stderr)
            continue
        fetched.append((feed_cfg["name"], items))

    all_items = [i for _, items in fetched for i in items]
    all_items.sort(key=lambda i: i.get("published", ""), reverse=True)

    # Only send items we have not delivered in a previous run. The feeds return
    # the same items every tick, so without this a 15-minute cron job re-sends
    # the whole backlog each time. We read `seen` BEFORE archiving this tick's
    # items, so a genuinely new item is still eligible on the tick that first
    # sees it. The identity is the link path, not raw_id — see store.dedup_key
    # for why (wire rewrites its raw_id on edits, blogroll has none, newsroom's
    # link rotates a utm_campaign).
    keys = {store.dedup_key(i) for i in all_items}
    seen = store.seen_keys(db, keys)
    new_items = [i for i in all_items if store.dedup_key(i) not in seen]

    sent = 0
    sent_items = []
    for chan_cfg in cfg["channels"]:
        selected = [i for i in new_items if matches(i, chan_cfg)]
        if not selected:
            continue
        body = render.digest(selected, chan_cfg)
        if dry_run:
            print(f"--- would send to {chan_cfg['name']} ---")
            print(body)
            continue
        channels.send(chan_cfg, body)
        sent += len(selected)
        sent_items.extend(selected)

    # Archive only what we actually delivered, and only after delivery, so a
    # failed run re-delivers on the next tick (cron retries) instead of marking
    # items seen without sending them. An item can match more than one channel,
    # so dedupe sent_items by key before writing. Skipped entirely in --dry-run
    # so a dry run never burns the backlog.
    if not dry_run:
        by_source = {}
        written = set()
        for item in sent_items:
            key = store.dedup_key(item)
            if key in written:
                continue
            written.add(key)
            by_source.setdefault(item["source"], []).append(item)
        for source, items in by_source.items():
            store.record_items(db, source, items)

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
