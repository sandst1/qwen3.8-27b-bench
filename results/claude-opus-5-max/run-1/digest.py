#!/usr/bin/env python3
"""notify-digest — poll a few feeds, filter them, send digests to channels.

Run from cron. See README.md.

Every run re-fetches the provider's current window, which still contains the
items we sent on the previous tick -- that is normal and is not something we
can ask the providers to stop doing. Suppressing the repeats is this script's
job, via the per-channel ledger in store.py:

    fetch -> filter by keyword -> drop what this channel already got
          -> render -> deliver -> only now, record it as delivered

The last two steps are in that order on purpose. See store.mark_sent.
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


def collect(cfg):
    """Fetch every feed. Returns (items, failed_feed_names).

    A feed that fails is skipped rather than aborting the run, so one broken
    provider does not stop the others being delivered. It is reported in the
    return value so the process can still exit non-zero.
    """
    items = []
    failed = []
    for feed_cfg in cfg["feeds"]:
        try:
            items.extend(feeds.fetch(feed_cfg))
        except feeds.FeedError as exc:
            print(f"warn: feed {feed_cfg['name']} failed: {exc}", file=sys.stderr)
            failed.append(feed_cfg["name"])
    items.sort(key=lambda i: i.get("published", ""), reverse=True)
    return items, failed


def run_once(cfg, db, dry_run=False, mark_seen=False):
    """One cron tick. Returns (items_delivered, failures).

    dry_run   -- render to stdout and touch nothing on disk.
    mark_seen -- record everything currently visible as already delivered,
                 without delivering it. Used to cut over to this version, and
                 to add a channel without back-filling it.
    """
    all_items, failures = collect(cfg)

    if not dry_run:
        store.record_items(db, all_items)

    sent = 0
    for chan_cfg in cfg["channels"]:
        name = chan_cfg["name"]
        selected = [i for i in all_items if matches(i, chan_cfg)]

        # The whole fix: everything this channel has already been delivered
        # drops out here, so a steady feed produces an empty digest and we
        # stay quiet instead of resending on every tick.
        fresh = store.unsent(db, name, selected)
        if not fresh:
            continue

        if mark_seen:
            store.mark_sent(db, name, fresh)
            print(f"marked {len(fresh)} item(s) as already seen for {name}")
            continue

        body = render.digest(fresh, chan_cfg)

        if dry_run:
            print(f"--- would send to {name} ---")
            print(body)
            continue

        try:
            channels.send(chan_cfg, body)
        except channels.DeliveryError as exc:
            # Do not mark these as sent: leaving them unrecorded is what makes
            # the next tick retry them. One dead channel must not stop the
            # others being delivered, so carry on and report at the end.
            print(f"warn: channel {name} failed: {exc}", file=sys.stderr)
            failures.append(name)
            continue

        store.mark_sent(db, name, fresh)
        sent += len(fresh)

    return sent, failures


def main(argv=None):
    ap = argparse.ArgumentParser(prog="digest")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--db", default="digest.sqlite3")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be sent; writes nothing to the database",
    )
    ap.add_argument(
        "--mark-seen",
        action="store_true",
        help="record everything currently in the feeds as already delivered,"
        " without sending it (cutover, or adding a channel quietly)",
    )
    args = ap.parse_args(argv)

    if args.dry_run and args.mark_seen:
        ap.error("--dry-run and --mark-seen are mutually exclusive")

    cfg = load_config(args.config)
    db = store.connect(args.db)
    try:
        sent, failures = run_once(
            cfg, db, dry_run=args.dry_run, mark_seen=args.mark_seen
        )
        print(f"sent {sent} items")
    finally:
        db.close()

    # Non-zero so cron/monitoring notices a feed or channel that is down.
    # The items involved were not marked delivered and will be retried.
    if failures:
        print(f"warn: {len(failures)} failure(s): {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
