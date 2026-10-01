#!/usr/bin/env python3
"""notify-digest — poll a few feeds, filter them, send digests to channels.

Run from cron. See README.md.

Each run sends only items that are new *for the receiving channel*: feeds
re-list their back-catalogue for as long as an item stays in their window,
and without that filter every 15-minute tick re-sent the same items. What
counts as "new" is tracked per channel in the SQLite `delivered` ledger
(see store.py), keyed on the item's normalised link rather than the id the
feed provides, because those ids change whenever an item is edited.
"""

import argparse
import sys
import tomllib

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
    """Build and send one round of digests; returns the number of items sent.

    Never touches the database in --dry-run mode: a preview must not
    consume the "new" status of the items it lists, and must not seed the
    ledger either.
    """
    if not dry_run:
        store.seed_ledger(db, [c["name"] for c in cfg["channels"]])

    all_items = []
    for feed_cfg in cfg["feeds"]:
        try:
            items = feeds.fetch(feed_cfg)
        except feeds.FeedError as exc:
            print(f"warn: feed {feed_cfg['name']} failed: {exc}", file=sys.stderr)
            continue
        if not dry_run:
            store.record_items(db, feed_cfg["name"], items)
        all_items.extend(items)

    all_items.sort(key=lambda i: i.get("published", ""), reverse=True)

    sent = 0
    for chan_cfg in cfg["channels"]:
        already = store.delivered_keys(db, chan_cfg["name"])
        selected = [
            i
            for i in all_items
            if (i["source"], store.identity(i["link"])) not in already
            and matches(i, chan_cfg)
        ]
        if not selected:
            continue
        body = render.digest(selected, chan_cfg)
        if dry_run:
            print(f"--- would send to {chan_cfg['name']} ---")
            print(body)
            continue
        channels.send(chan_cfg, body)
        # Record only after a successful send, and commit per channel: a
        # failing webhook crashes the run (see channels.py), and the items
        # not yet marked for that channel are retried on the next tick.
        store.mark_delivered(db, chan_cfg["name"], selected)
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
