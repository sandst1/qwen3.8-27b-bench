#!/usr/bin/env python3
"""notify-digest — poll a few feeds, filter them, send digests to channels.

Run from cron. See README.md.

Because cron runs this every 15 minutes and feeds hold items for days, the
central job here is deciding what is *new*. Identity is decided in
`identity.py`; the record of what has already gone out lives in `store.py`.
The rule this loop is built around: an item is only written to the ledger
after the channel has accepted it, so a failed send is retried on the next
tick rather than lost.
"""

import argparse
import sys
import tomllib

import channels
import feeds
import identity
import render
import store

# Entries are forgotten only after the item has been absent from the feeds
# for this long -- see store.prune. Generous on purpose: the cost of a large
# ledger is a few MB, the cost of forgetting too early is resending.
DEFAULT_RETENTION_DAYS = 90


class ConfigError(Exception):
    pass


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


def collect(cfg, db, record=True):
    """Fetch every feed and return the items, newest first.

    A feed that fails is skipped with a warning, as before. Note the
    interaction with dedup: items from a failed feed are simply absent this
    poll, so their ledger entries are not refreshed. That is harmless unless
    the feed stays down for the entire retention window.
    """
    all_items = []
    for feed_cfg in cfg["feeds"]:
        try:
            items = feeds.fetch(feed_cfg)
        except feeds.FeedError as exc:
            print(f"warn: feed {feed_cfg['name']} failed: {exc}", file=sys.stderr)
            continue
        for item in items:
            item["idents"] = identity.keys_for(item)
        if record:
            store.record_items(db, feed_cfg["name"], items)
        all_items.extend(items)

    all_items.sort(key=lambda i: i.get("published", ""), reverse=True)

    # Collapse items that are already duplicates within this single poll --
    # the same story carried twice by one feed, or a feed listed twice in the
    # config. Without this a digest can show the same link twice in one
    # message. Cross-source duplicates (the same story from two different
    # publishers, at different URLs) are deliberately left alone; matching
    # those is fuzzy text comparison and a different problem.
    seen = set()
    unique = []
    for item in all_items:
        primary = item["idents"][0]
        if primary in seen:
            continue
        seen.add(primary)
        unique.append(item)
    return unique


def validate(cfg):
    """Check the things the ledger depends on before we touch the database.

    The channel `name` is the ledger key, which makes it load-bearing in a way
    it was not before. Two config mistakes now have non-obvious consequences,
    so they are caught here rather than discovered from a silent digest:

      * duplicate names share a ledger, so the second channel would suppress
        items on the strength of the first channel's deliveries;
      * a missing name has nothing to key on at all.

    Renaming a channel is the third case and cannot be detected here: the new
    name has no history, so the channel re-sends everything currently on the
    feeds once. `--seed` after a rename avoids that. This is noted in README.
    """
    seen = set()
    for chan_cfg in cfg.get("channels", []):
        name = chan_cfg.get("name")
        if not name:
            raise ConfigError("every channel needs a name; it keys the delivery ledger")
        if name in seen:
            raise ConfigError(
                f"duplicate channel name {name!r}: channels share a ledger by name, "
                "so the second would be silently deduped against the first"
            )
        seen.add(name)


def run_once(cfg, db, dry_run=False, seed=False, retention_days=DEFAULT_RETENTION_DAYS):
    """One poll. Returns (items_sent, failed_channel_count)."""
    validate(cfg)
    all_items = collect(cfg, db, record=not dry_run)

    sent = 0
    failures = 0

    for chan_cfg in cfg["channels"]:
        name = chan_cfg["name"]
        matched = [i for i in all_items if matches(i, chan_cfg)]
        if not matched:
            continue

        fresh, repeats = [], []
        for item in matched:
            (repeats if store.already_delivered(db, name, item["idents"]) else fresh).append(item)

        if not dry_run and repeats:
            # Re-record the repeats. This keeps their last_seen current so
            # pruning cannot forget an item that is still on the feed, and
            # links any key the item has newly grown (a rotated guid, a moved
            # URL) to the identity we already know.
            for item in repeats:
                store.mark_delivered(db, name, item["idents"])
            db.commit()

        if not fresh:
            continue

        if seed:
            # Adopt the current feed contents without sending, so deploying
            # dedup onto a running install does not fire one last duplicate
            # digest at everybody.
            for item in fresh:
                store.mark_delivered(db, name, item["idents"])
            db.commit()
            print(f"seed: {name}: marked {len(fresh)} item(s) as already delivered")
            continue

        body = render.digest(fresh, chan_cfg)

        if dry_run:
            print(f"--- would send to {name} ({len(fresh)} new) ---")
            print(body)
            continue

        try:
            channels.send(chan_cfg, body)
        except Exception as exc:
            # Isolate the failure. Previously one unreachable webhook aborted
            # the run, so every channel configured after it went silent too,
            # and it stayed that way for as long as the webhook was down.
            #
            # Nothing is marked delivered, so these items go out on the next
            # tick. The exit code is non-zero so the failure is still visible
            # to whatever watches cron.
            print(
                f"error: channel {name} failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            failures += 1
            continue

        # Only after the channel has accepted it. Marking before the send
        # would mean a failed delivery silently loses the item forever, which
        # is a worse bug than the one being fixed.
        for item in fresh:
            store.mark_delivered(db, name, item["idents"])
        db.commit()
        sent += len(fresh)

    if not dry_run:
        dropped = store.prune(db, retention_days)
        if dropped:
            print(f"pruned {dropped} ledger entr{'y' if dropped == 1 else 'ies'}")

    return sent, failures


def main(argv=None):
    ap = argparse.ArgumentParser(prog="digest")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--db", default="digest.sqlite3")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be sent; writes nothing, so repeated runs agree",
    )
    ap.add_argument(
        "--seed",
        action="store_true",
        help="mark everything currently on the feeds as already delivered, "
        "without sending. Run once when deploying onto an existing install.",
    )
    ap.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help="forget a ledger entry once its item has been absent from the "
        f"feeds this long (default: {DEFAULT_RETENTION_DAYS}; 0 disables)",
    )
    args = ap.parse_args(argv)

    if args.dry_run and args.seed:
        ap.error("--dry-run and --seed are contradictory: seeding is a write")

    cfg = load_config(args.config)
    try:
        validate(cfg)
    except ConfigError as exc:
        print(f"error: {args.config}: {exc}", file=sys.stderr)
        return 2

    db = store.connect(args.db)
    try:
        sent, failures = run_once(
            cfg,
            db,
            dry_run=args.dry_run,
            seed=args.seed,
            retention_days=args.retention_days,
        )
        if not args.seed:
            print(f"sent {sent} new item(s)")
    finally:
        db.close()

    # Non-zero if any channel refused, so cron surfaces it. The items are not
    # lost; they are simply still unmarked and will go out next tick.
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
