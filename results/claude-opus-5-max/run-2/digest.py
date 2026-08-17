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


def run_once(cfg, db, dry_run=False, seed=False):
    """Poll every feed and send each channel the items it has not had yet.

    Feeds keep returning items for as long as the provider lists them, so
    "what is in the feed" is not the same question as "what is new". The
    difference is the `deliveries` ledger in store.py, consulted per channel.

    Delivery is at-least-once: the ledger is written *after* channels.send()
    returns, so a channel that is down repeats its digest on the next tick
    rather than silently swallowing it. Marking first would turn an outage into
    "items vanish", which is far harder to notice than a duplicate.
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
        selected = [i for i in all_items if matches(i, chan_cfg)]
        fresh = store.unsent(db, name, selected)
        if not fresh:
            continue

        if seed:
            # Catching up an existing deployment: mark as delivered without
            # sending, so the backlog already sitting in the feeds is not
            # announced all over again.
            store.mark_sent(db, name, fresh)
            sent += len(fresh)
            continue

        body = render.digest(fresh, chan_cfg)
        if dry_run:
            # Deliberately no mark_sent: a dry run must not change what a
            # subsequent real run would deliver.
            print(f"--- would send to {name} ---")
            print(body)
            continue

        try:
            channels.send(chan_cfg, body)
        except channels.DeliveryError as exc:
            # Isolate the failure: one broken webhook should not stop the
            # remaining channels from getting their digests. These items stay
            # unmarked and are retried on the next cron tick.
            print(f"warn: channel {name} failed: {exc}", file=sys.stderr)
            continue

        store.mark_sent(db, name, fresh)
        sent += len(fresh)

    return sent


def main(argv=None):
    ap = argparse.ArgumentParser(prog="digest")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--db", default="digest.sqlite3")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--seed",
        action="store_true",
        help="mark everything currently in the feeds as already delivered,"
        " without sending it; run once when deploying against an existing db",
    )
    args = ap.parse_args(argv)

    if args.seed and args.dry_run:
        ap.error("--seed and --dry-run are mutually exclusive")

    cfg = load_config(args.config)
    db = store.connect(args.db)
    try:
        sent = run_once(cfg, db, dry_run=args.dry_run, seed=args.seed)
        if args.seed:
            print(f"seeded {sent} items as already delivered")
        else:
            print(f"sent {sent} items")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
