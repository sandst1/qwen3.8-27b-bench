"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None}

`link` is the item's identity for dedup (see store.py). It is normalised
with `normalise_link` so that feeds which rotate tracking parameters
(e.g. utm_campaign) between polls cannot cause the same story to be
delivered twice.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class FeedError(Exception):
    pass


def normalise_link(url):
    """Strip utm_* query params so links stay stable across polls.

    The newsroom feed rotates utm_campaign every poll; without this the
    same story would look like a new item each time.
    """
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    kept = [
        (k, v)
        for k, v in query
        if not k.lower().startswith("utm_")
    ]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), parts.fragment)
    )


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
                "link": normalise_link(r["url"]),
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
                "link": normalise_link(r["permalink"]),
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
                "link": normalise_link(r["link"]),
            "summary": r.get("description", ""),
            "published": r.get("pubDate", ""),
            "source": name,
            "raw_id": r.get("guid"),
        }
        for r in doc.get("items", [])
    ]
