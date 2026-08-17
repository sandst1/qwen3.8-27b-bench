"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "identity": str}

`raw_id` is the identifier the provider sent, kept verbatim for the
archive. `identity` is the key digest.py uses to tell "same item" across
runs, and it is deliberately not always the same as `raw_id`, because each
provider's natural id has a flaw:

- newsroom: entry_id is stable, so identity is the entry_id. (The URLs are
  not usable: they carry a ?utm_campaign= value that changes every poll.)
- blogroll: there is no identifier at all, and titles/excerpts change when
  a post is edited, so identity is the permalink — the only stable field.
- generic (wire): the guid is regenerated whenever an item is edited
  (typos, retitles), so identity is the link, not the guid.

Identity is namespaced with the feed name so two feeds carrying the same
URL can never collide.
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
                "identity": f"{name}:{r['entry_id']}",
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
                "identity": f"{name}:{r['permalink']}",
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
            "identity": f"{name}:{r['link']}",
        }
        for r in doc.get("items", [])
    ]
