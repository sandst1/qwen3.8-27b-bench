"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None}

Item identity (see item_key)
----------------------------
We need a key that says "this is the same article as one we already sent",
and none of the raw fields give us that on their own:

  * newsroom  entry_id is stable, but `link` carries a `utm_campaign` that
              the provider rotates week to week (w33 -> w34), so the raw
              link is NOT stable.
  * blogroll  has no id at all; the `permalink` is stable but the title and
              excerpt get quietly edited.
  * wire      has a guid, but the provider regenerates it every time an item
              is edited, so the guid is NOT stable.

The one field that stays constant for the same article across all three
providers is the link once marketing/tracking query params are stripped.
item_key() builds the key from that normalised link, falling back to raw_id
and then title+published if a feed ever omits the link. Keys are always
compared scoped by `source`, so two feeds pointing at the same URL stay
distinct.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class FeedError(Exception):
    pass


# Query params that providers append for campaign tracking. They vary between
# pulls of the *same* article, so they must not take part in the identity.
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref"}


def _normalize_link(link):
    """Lower-case scheme/host and drop tracking query params."""
    if not link:
        return ""
    parts = urlsplit(link.strip())
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower().startswith(_TRACKING_PARAM_PREFIXES) or k.lower() in _TRACKING_PARAMS)
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(kept), parts.fragment))


def item_key(item):
    """Stable per-source identity for an item.

    Returns a string that is identical for two pulls of the same article even
    when the provider rotated the link's tracking params or regenerated the
    guid. Combine with item["source"] when comparing/storing.
    """
    link = _normalize_link(item.get("link", ""))
    if link:
        return link
    if item.get("raw_id"):
        return f"id:{item['raw_id']}"
    return f"title:{(item.get('title') or '').strip()}|{item.get('published') or ''}"


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
            }
            for r in rows
        ]

    if fmt == "blogroll":
        # No stable identifier of any kind in this one.
        return [
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

    # "generic": has a guid, but the provider regenerates it whenever an
    # item is edited (typos, added tags, retitles).
    return [
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
