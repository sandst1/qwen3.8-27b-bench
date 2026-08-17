"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "dedupe_key": str}

`dedupe_key` is what digest.py/store.py use to decide "have we already
notified for this item?" (see store.filter_unseen). It is deliberately
*not* always `raw_id`, because the three providers' ids have wildly
different reliability -- see `_dedupe_key` below for the reasoning per
format. Getting this wrong is exactly how you get "the same item every
15 minutes" (stable content, unstable id) or "the same item never again"
(unstable content, id reused) bugs, so if you add a fourth provider,
think through this rather than defaulting to `raw_id`.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class FeedError(Exception):
    pass


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


def _canonical_link(link):
    """Strip the query string and fragment off a link.

    newsroom embeds campaign-tracking params (utm_source, utm_campaign, ...)
    that change on every export even when the article itself hasn't -- see
    the snapshot-a vs snapshot-b fixtures, same entry_id, different
    utm_campaign. Comparing raw links would make that look like a new item
    on every poll. Stripping query/fragment gives a link we can actually
    compare across fetches.
    """
    parts = urllib.parse.urlsplit(link)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _dedupe_key(source, fmt, item):
    """Pick the most stable identity we have for this item.

    Per format, based on what we know about each provider (see fixtures/
    for worked examples of each):

    - newsroom: `entry_id` is a real backing-store primary key. It survives
      retitles/edits and is the one raw_id we actually trust, so we key on
      it directly. (Its link is *not* trustworthy -- see _canonical_link.)
    - blogroll: no id of any kind is provided, ever. The permalink is the
      only thing to key on, so it has to be treated as immutable identity;
      title/excerpt edits to the same permalink are treated as the same
      item, not a new one.
    - generic (wire): does have a guid, but the provider regenerates it on
      every edit (typos, added tags, retitles), so treating it as identity
      would mean a punctuation fix gets re-sent as "new". The link is the
      stable part here, so we key on that instead and ignore the guid.

    Keys are namespaced by source name so two feeds can never collide even
    if two providers happen to reuse the same id or link scheme.
    """
    if fmt == "newsroom":
        return f"{source}:id:{item['raw_id']}"
    return f"{source}:link:{_canonical_link(item['link'])}"


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
        # item is edited (typos, added tags, retitles).
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
        item["dedupe_key"] = _dedupe_key(name, fmt, item)
    return items
