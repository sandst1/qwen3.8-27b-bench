"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "identity": str}

`identity` is the value we dedupe deliveries on, and it is deliberately
*not* always the same underlying field, because no single field is stable
across all three providers (verified against fixtures/snapshot-a vs -b):

    newsroom  -> entry_id.  Stable, but the URL is not: it carries a
                 rotating utm_campaign parameter, so `link` would look
                 like a brand-new item every campaign.
    blogroll  -> permalink.  There is no id at all; the title and excerpt
                 do get edited, but the permalink never changes.
    wire      -> link.       The guid is regenerated whenever the provider
                 edits an item, so it would re-trigger delivery on typo
                 fixes; the link is stable.

If we ever add a provider, pick its identity here, at the point where the
provider's quirks are known — nowhere else.
"""

import json
import urllib.error
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


def fetch(feed_cfg):
    doc = _load(feed_cfg)
    name = feed_cfg["name"]
    fmt = feed_cfg.get("format", "generic")

    if fmt == "newsroom":
        rows = doc.get("entries", [])
        return [
            {
                "title": r["headline"],
                "link": r["url"],
                "summary": r.get("standfirst", ""),
                "published": r.get("published_at", ""),
                "source": name,
                "raw_id": str(r["entry_id"]),
                "identity": str(r["entry_id"]),
            }
            for r in rows
        ]

    if fmt == "blogroll":
        # No id of any kind; the permalink is the only stable field.
        return [
            {
                "title": r["title"],
                "link": r["permalink"],
                "summary": r.get("excerpt", ""),
                "published": r.get("date", ""),
                "source": name,
                "raw_id": None,
                "identity": r["permalink"],
            }
            for r in doc.get("posts", [])
        ]

    # "generic": has a guid, but the provider regenerates it whenever an
    # item is edited (typos, added tags, retitles), so we key on the link.
    return [
        {
            "title": r["title"],
            "link": r["link"],
            "summary": r.get("description", ""),
            "published": r.get("pubDate", ""),
            "source": name,
            "raw_id": r.get("guid"),
            "identity": r["link"],
        }
        for r in doc.get("items", [])
    ]


def item_key(item):
    """Globally unique key for an item: identity namespaced by source.

    Used as the dedupe key in the `sent` table (see store.py / digest.py).
    """
    return f"{item['source']}\x1f{item['identity']}"
