"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None}
"""

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


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


# Query params that track analytics/referrals but do not identify the content.
# The same article is served under different utm_campaign values run to run
# (see fixtures/snapshot-a vs snapshot-b newsroom), so these must be stripped
# before we compare two links.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "gclsrc",
        "wbraid",
        "dclid",
        "googleclid",
    }
)


def normalize_link(link):
    """Return the part of a URL that identifies the content, dropping noise.

    Keeps scheme + host + path and any non-tracking query params; drops the
    fragment and any analytics/referral parameters.
    """
    scheme, netloc, path, query, _fragment = urlsplit(link)
    keep = [
        part
        for part in query.split("&")
        if part.split("=", 1)[0].lower() not in _TRACKING_PARAMS
    ]
    return urlunsplit((scheme, netloc, path, "&".join(keep), ""))


def identity(item):
    """Stable identity for an item, used to decide whether we've seen it.

    Returns (source, normalized_link). We key on the link rather than each
    feed's own id because those ids are not stable across runs:

      - newsroom: entry_id is stable, but the URL carries volatile utm params
        (stripped by normalize_link).
      - blogroll: has no server-side id at all; the permalink is stable.
      - wire: guid is regenerated whenever the item is edited, but the link is
        not, so a rewritten wire story must still be recognised as the same
        item.
    """
    return (item["source"], normalize_link(item["link"]))
