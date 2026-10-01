"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None}

Items are deduplicated across runs via item_key(); see store.py for how
delivery history is kept.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


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


def item_key(item):
    """Stable identity of an item across fetches.

    Keyed on source + link host/path, because neither field you would
    naively reach for is stable in our feeds:

    - the generic provider regenerates the guid whenever an item is edited
      (see "wire-...-r2" in fixtures/snapshot-b), so guid == new is wrong;
    - the newsroom rotates the utm_* tracking params in its URLs between
      snapshots, so the full URL (and its hash) is not stable either.

    An item re-published at the same URL after an edit is deliberately not
    treated as new news. Feeds that ship an empty link fall back to
    raw_id/title so their keys stay distinct instead of colliding on "".
    """
    link = (item.get("link") or "").strip()
    if link:
        parts = urlsplit(link)
        ident = f"{parts.netloc.lower()}{parts.path.rstrip('/')}"
    else:
        ident = item.get("raw_id") or item.get("title", "")
    return f"{item['source']}|{ident}"
