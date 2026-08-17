#!/usr/bin/env python3
"""notify-digest — poll a few feeds, filter them, send digests to channels.

Run from cron every 15 minutes.  See README.md.

── Deduplication ────────────────────────────────────────────────────────────
Each item is assigned a stable fingerprint by store.fingerprint() (see
store.py for the full rationale).  Before building a digest for a channel we
drop any item whose fingerprint has already been recorded as sent to that
channel.  After a successful delivery we record the fingerprints so they are
skipped on the next run.

We record *per channel* rather than globally because different channels may
have different subscriber lists; an item sent to "ops" should still be sent
to "energy" if it matches there too.

Fingerprints are written only after channels.send() succeeds.  If delivery
fails (network error, bad HTTP status) the item is not marked sent, so the
next cron run will retry it — consistent with the existing behaviour where
delivery errors are fatal and cron retries the whole job.
────────────────────────────────────────────────────────────────────────────
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

    # Attach a fingerprint to every item once, up front, so we don't
    # recompute it in the inner loop below.
    for item in all_items:
        item["_fp"] = store.fingerprint(item)

    all_items.sort(key=lambda i: i.get("published", ""), reverse=True)

    sent = 0
    for chan_cfg in cfg["channels"]:
        channel_name = chan_cfg["name"]

        # Filter by keyword, then drop anything already sent to this channel.
        selected = [
            i for i in all_items
            if matches(i, chan_cfg) and not store.already_sent(db, i["_fp"], channel_name)
        ]
        if not selected:
            continue

        body = render.digest(selected, chan_cfg)
        if dry_run:
            print(f"--- would send to {channel_name} ({len(selected)} new item(s)) ---")
            print(body)
            # In dry-run mode we intentionally do NOT mark items as sent so
            # that a real run afterwards still delivers them.
            continue

        channels.send(chan_cfg, body)

        # Mark items sent only after successful delivery so that a delivery
        # failure causes the next cron run to retry rather than silently drop.
        for item in selected:
            store.mark_sent(db, item["_fp"], channel_name)

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
