"""Stable identity for feed items.

This exists because none of the "obvious" identifiers survive contact with
our providers (see the fixtures/ snapshots for examples):

  * newsroom  has a stable `entry_id`, but its URLs carry a `utm_campaign`
              parameter that rotates every week, so the raw link changes
              even when the story does not.
  * wire      has a stable link, but regenerates its `guid` every time an
              item is edited (`...-0031` becomes `...-0031-r2`).
  * blogroll  has neither: no id at all, only a permalink, which is stable.

So we key items on the *normalised* link: same link once you strip the
tracking noise means the same story, even if the provider retitled it or
bumped its guid. An edited story is deliberately NOT re-sent; people
complained about duplicates far more than they asked for update notices.

Fallbacks, for items that somehow have no link: raw_id, then a hash of
title + published. These are last resorts, not good identities — the wire
guid churn shows why.
"""

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that providers append for tracking and that change
# between fetches of the very same story.
_TRACKING_PREFIXES = ("utm_", "mc_", "ref")


def normalize_link(link):
    """Reduce a URL to its canonical form for identity purposes."""
    parts = urlsplit(link.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            urlencode(query),
            "",  # drop the fragment
        )
    )


def item_key(item):
    """Return the dedup key for a normalised feed item."""
    source = item["source"]
    link = item.get("link")
    if link:
        return f"{source}|{normalize_link(link)}"
    if item.get("raw_id"):
        return f"{source}#raw|{item['raw_id']}"
    digest = hashlib.sha256(
        f"{item['title']}|{item.get('published') or ''}".encode()
    ).hexdigest()
    return f"{source}#hash|{digest}"
