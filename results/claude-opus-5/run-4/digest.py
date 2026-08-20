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
    seen_keys = set()
    for feed_cfg in cfg["feeds"]:
        try:
            items = feeds.fetch(feed_cfg)
        except feeds.FeedError as exc:
            print(f"warn: feed {feed_cfg['name']} failed: {exc}", file=sys.stderr)
            continue
        store.record_items(db, feed_cfg["name"], items)
        for item in items:
            # A feed can list the same item twice in one document, and a feed
            # can be configured twice by mistake. Collapse within the run too,
            # or the digest shows it twice on its first (and only) outing.
            key = (item["source"], item["dedupe_key"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_items.append(item)

    all_items.sort(key=lambda i: i.get("published", ""), reverse=True)

    sent = 0
    for chan_cfg in cfg["channels"]:
        name = chan_cfg["name"]
        selected = [i for i in all_items if matches(i, chan_cfg)]

        # The feeds hand us the same items on every 15-minute tick, so filter
        # out what this channel has already been sent. Per channel, not
        # globally: an item can belong in several digests.
        seen = store.already_delivered(db, name, selected)
        fresh = [i for i in selected if (i["source"], i["dedupe_key"]) not in seen]

        if not fresh:
            continue

        body = render.digest(fresh, chan_cfg)
        if dry_run:
            print(f"--- would send to {name} ---")
            print(body)
            continue

        try:
            channels.send(chan_cfg, body)
        except channels.DeliveryError as exc:
            # Don't record the delivery: the next tick retries this channel.
            # Keep going so one broken webhook doesn't starve the others.
            print(f"warn: channel {name} failed: {exc}", file=sys.stderr)
            continue

        # Recorded only after a successful send. A crash in this gap re-sends
        # the digest once on the next tick; recording first would instead lose
        # it silently and forever. For a digest, a rare repeat beats a drop.
        store.record_delivery(db, name, fresh)
        sent += len(fresh)

    if not dry_run:
        store.prune_deliveries(db)

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
