"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "dedupe_key": str}

`raw_id` is whatever identifier the provider gave us, kept only for
reference. `dedupe_key` is what the rest of the app should use to decide
"have we seen this before?" -- see the comment on dedupe_key() for why it
isn't just `raw_id`.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class FeedError(Exception):
    pass


# Query-string params that describe how a reader arrived at a link, not what
# the link points to. Providers regenerate these on every fetch (fresh
# campaign tag, etc.) even when the article itself hasn't changed, so they
# must be stripped before a link can be used as a stable identity.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid",
}


def normalize_link(url):
    """Canonicalise a link for identity purposes: drop tracking params and
    any fragment, and sort what's left so param order can't matter either."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode(sorted(
        (k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ))
    return urllib.parse.urlunsplit(parts._replace(query=query, fragment=""))


def dedupe_key(source, item):
    """The identity we key "already seen / already sent" state on.

    We deliberately do NOT use the provider's raw_id here:

    - blogroll gives us no id at all.
    - generic (wire) gives us a guid, but it regenerates it whenever the
      item is edited (see fetch() below), so keying on it would treat every
      correction as a brand new item and re-notify people for it.
    - newsroom's id is actually stable, but its link isn't (tracking params
      change run to run) -- so we can't use raw link either.

    A source-qualified, tracking-params-stripped link turns out to be
    stable across all three feed formats and is always present, so it's
    the one thing we can rely on everywhere.
    """
    return f"{source}:{normalize_link(item['link'])}"


def _load(feed_cfg):
    url = feed_cfg["url"]
    if url.startswith("file://") or not url.startswith("http"):
        path = Path(url.replace("file://", ""))
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise FeedError(str(exc)) from exc
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        raise FeedError(str(exc)) from exc


def fetch(feed_cfg):
    doc = _load(feed_cfg)
    name = feed_cfg["name"]
    fmt = feed_cfg.get("format", "generic")

    if fmt == "newsroom":
        rows = doc.get("entries", [])
        items = [
            {
                "title": r["headline"],
                "link": r["url"],
                "summary": r.get("standfirst", ""),
                "published": r.get("published_at", ""),
                "source": name,
                "raw_id": str(r["entry_id"]),
            }
            for r in rows
        ]

    elif fmt == "blogroll":
        # No stable identifier of any kind in this one.
        items = [
            {
                "title": r["title"],
                "link": r["permalink"],
                "summary": r.get("excerpt", ""),
                "published": r.get("date", ""),
                "source": name,
                "raw_id": None,
            }
            for r in doc.get("posts", [])
        ]

    else:
        # "generic": has a guid, but the provider regenerates it whenever an
        # item is edited (typos, added tags, retitles), so it's not usable
        # as a stable identity -- see dedupe_key().
        items = [
            {
                "title": r["title"],
                "link": r["link"],
                "summary": r.get("description", ""),
                "published": r.get("pubDate", ""),
                "source": name,
                "raw_id": r.get("guid"),
            }
            for r in doc.get("items", [])
        ]

    for item in items:
        item["dedupe_key"] = dedupe_key(name, item)
    return items
