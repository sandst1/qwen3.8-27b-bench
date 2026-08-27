"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "key": str}

`key` is the stable identity used for dedup; see identity.py for why it is
neither `raw_id` nor the raw link.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

import identity


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
        item["key"] = identity.item_key(item)
    return items
