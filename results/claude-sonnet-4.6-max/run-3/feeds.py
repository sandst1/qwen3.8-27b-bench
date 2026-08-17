"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None}

The `link` field is always a canonical URL: UTM tracking parameters are
stripped before the item leaves this module.  This matters because the
newsroom provider rotates utm_campaign every week (w33 → w34 …), which
would otherwise make the same article look new to the deduplication layer
on every poll.  See _canonical_link() below.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Query-string keys that are tracking noise, not part of the article identity.
# Extend this list if new providers introduce their own tracking parameters.
_UTM_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign",
    "utm_term", "utm_content", "utm_id",
})


def _canonical_link(url):
    """Return *url* with UTM tracking parameters removed.

    Example:
        https://newsroom.example/article?utm_source=feed&utm_campaign=w34
        → https://newsroom.example/article
    """
    if not url:
        return url
    parsed = urllib.parse.urlparse(url)
    clean_qs = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parsed.query)
        if k not in _UTM_PARAMS
    ]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(clean_qs)))


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
                "link": _canonical_link(r["url"]),
                "summary": r.get("standfirst", ""),
                "published": r.get("published_at", ""),
                "source": name,
                "raw_id": str(r["entry_id"]),
            }
            for r in rows
        ]

    if fmt == "blogroll":
        # No stable identifier of any kind in this one; permalink is the key.
        return [
            {
                "title": r["title"],
                "link": _canonical_link(r["permalink"]),
                "summary": r.get("excerpt", ""),
                "published": r.get("date", ""),
                "source": name,
                "raw_id": None,
            }
            for r in doc.get("posts", [])
        ]

    # "generic": has a guid, but the provider regenerates it whenever an
    # item is edited (typos, added tags, retitles).  Use link, not guid.
    return [
        {
            "title": r["title"],
            "link": _canonical_link(r["link"]),
            "summary": r.get("description", ""),
            "published": r.get("pubDate", ""),
            "source": name,
            "raw_id": r.get("guid"),
        }
        for r in doc.get("items", [])
    ]
