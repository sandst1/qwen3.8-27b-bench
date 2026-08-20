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
    """One cron tick: fetch, filter per channel, send what has not been sent.

    `seed` records everything as delivered without sending it — see main().
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
    failed = False
    for chan_cfg in cfg["channels"]:
        name = chan_cfg["name"]
        matching = [i for i in all_items if matches(i, chan_cfg)]

        # The whole point of the exercise: drop anything this channel has
        # already been sent. Deduplicated by key first, because a single feed
        # can legitimately carry the same item twice in one document.
        already = store.delivered_keys(db, name, [i["key"] for i in matching])
        selected, seen_this_run = [], set()
        for item in matching:
            if item["key"] in already or item["key"] in seen_this_run:
                continue
            seen_this_run.add(item["key"])
            selected.append(item)

        if not selected:
            continue

        keys = [i["key"] for i in selected]

        if seed:
            store.mark_delivered(db, name, keys)
            print(f"seeded {len(keys)} item(s) for {name} (not sent)")
            continue

        if dry_run:
            print(f"--- would send to {name} ---")
            print(render.digest(selected, chan_cfg))
            continue

        try:
            channels.send(chan_cfg, render.digest(selected, chan_cfg))
        except channels.DeliveryError as exc:
            # Isolate the failure: one unreachable webhook must not stop the
            # remaining channels, which would leave them permanently behind
            # and resending on every tick.
            print(f"warn: channel {name} failed: {exc}", file=sys.stderr)
            failed = True
            continue

        # Only after a successful send. See store.py on at-least-once.
        store.mark_delivered(db, name, keys)
        sent += len(selected)

    return sent, failed


def main(argv=None):
    ap = argparse.ArgumentParser(prog="digest")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--db", default="digest.sqlite3")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be sent; records nothing")
    ap.add_argument("--seed", action="store_true",
                    help="mark everything currently in the feeds as already "
                         "delivered, without sending it. Use when adding a "
                         "channel so it starts from now instead of receiving "
                         "the entire backlog.")
    args = ap.parse_args(argv)

    if args.seed and args.dry_run:
        ap.error("--seed and --dry-run are mutually exclusive")

    cfg = load_config(args.config)
    db = store.connect(args.db)
    try:
        sent, failed = run_once(cfg, db, dry_run=args.dry_run, seed=args.seed)
        if not (args.dry_run or args.seed):
            print(f"sent {sent} items")
    finally:
        db.close()
    # Non-zero on delivery failure so cron surfaces it rather than logging
    # quietly into a file nobody reads.
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
