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
from urllib.parse import urlsplit


class FeedError(Exception):
    pass


def item_key(item):
    """Stable identity of an item, used to avoid re-sending it. See README.

    Deliberately NOT raw_id:

    - blogroll has no id at all (raw_id is None),
    - the generic provider regenerates its guid whenever an item is edited,
      so an edited story would look brand new and be re-sent,
    - newsroom ids are stable, but one rule for all providers is simpler.

    The URL path is the one field every provider keeps stable. We drop the
    query string and fragment because the newsroom feed appends rotating
    campaign parameters (?utm_campaign=w34) that would otherwise make the
    same story look new every week. None of our three providers
    distinguishes items by query string; if one ever does, revisit this.
    """
    parts = urlsplit((item.get("link") or "").strip())
    path = parts.path.rstrip("/").lower()
    return f"{item.get('source', '')}|{parts.scheme.lower()}|{parts.netloc.lower()}|{path}"


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
