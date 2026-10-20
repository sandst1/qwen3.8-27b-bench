"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

    The three providers we currently pull from do not agree on much, so this
    module normalises them into a common item shape:

        {"title": str, "link": str, "summary": str, "published": str,
         "source": str, "raw_id": str | None, "key": str}

    `key` is the item's stable identity, used to dedup across runs. Each
    provider hands us a different stable anchor, so it is set per format below
    rather than with a one-size-fits-all rule — see those notes.
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
                # entry_id is stable across edits; the url is not (the provider
                # re-appends utm params), so entry_id is the dedup anchor.
                "key": str(r["entry_id"]),
            }
            for r in rows
        ]

    if fmt == "blogroll":
        # No provider id here at all, and titles/excerpts get edited in place.
        # The permalink is the only thing that stays put, so dedup on the link.
        return [
            {
                "title": r["title"],
                "link": r["permalink"],
                "summary": r.get("excerpt", ""),
                "published": r.get("date", ""),
                "source": name,
                "raw_id": None,
                "key": r["permalink"],
            }
            for r in doc.get("posts", [])
        ]

    # "generic": has a guid, but the provider regenerates it whenever an
    # item is edited (typos, added tags, retitles) — e.g. "wire-...-0031"
    # becomes "wire-...-0031-r2". That makes guid useless for dedup; the link
    # is the stable anchor, so key on it despite raw_id being populated.
    return [
        {
            "title": r["title"],
            "link": r["link"],
            "summary": r.get("description", ""),
            "published": r.get("pubDate", ""),
            "source": name,
            "raw_id": r.get("guid"),
            "key": r["link"],
        }
        for r in doc.get("items", [])
    ]
