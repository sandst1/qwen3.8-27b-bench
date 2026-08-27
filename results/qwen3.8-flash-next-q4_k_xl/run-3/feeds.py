"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "uid": str}

Identifying items across runs
-----------------------------
Each item gets a `uid` ("<source>|<key>") used to decide what counts as
"already seen". The providers make this awkward, so the key is chosen
per feed via the `dedupe_by` config option:

    newsroom  entry_id is stable, but the URL carries a utm_campaign that
              rotates weekly -> dedupe_by = "raw_id"
    generic   guid is regenerated whenever an item is edited (see the two
              wire fixtures), while the link never changes -> dedupe_by = "link"
    blogroll  no identifier of any kind -> dedupe_by = "link"

The default is "link", which is the only field present and stable in every
format; a feed sets "raw_id" only when its provider guarantees the id. If
raw_id is missing we fall back to link.

Consequence we accept on purpose: an edited item (retitled blog post,
corrected wire story) is NOT re-sent. Missing an edit is better than
re-notifying everyone every time a provider touches a story.
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
    items = _normalize(doc, name, fmt)
    _attach_uids(items, feed_cfg)
    return items


def _normalize(doc, name, fmt):
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

    # "generic"
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


def _attach_uids(items, feed_cfg):
    by = feed_cfg.get("dedupe_by", "link")
    for item in items:
        key = item["raw_id"] if by == "raw_id" and item["raw_id"] else item["link"]
        item["uid"] = f"{item['source']}|{key}"
