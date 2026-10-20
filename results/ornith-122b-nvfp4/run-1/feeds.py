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


def _canonical_link(link):
    """The article URL with query and fragment stripped.

    The newsroom feed rewrites its query every week (utm_campaign rolls over),
    so the raw link is not stable even though the article is. Dropping the
    query leaves the stable scheme+host+path. If a feed ever encodes identity
    in a query param this would miss it; none of ours do.
    """
    parts = urlsplit(link or "")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def item_key(item):
    """Stable identity for an item across runs, used to skip re-sends.

    The three feeds cannot agree on an identifier: newsroom has a stable
    entry_id (raw_id), the wire regenerates its guid on every edit, and the
    blogroll has no id at all. The one field present and stable across all
    three is the article URL, so we key on (source, canonical link).

    Consequences, so the next reader knows what to expect:
      - The same story is delivered to a given channel only once, ever.
      - An *edited* story (new excerpt, regenerated guid, "(updated)" title)
        keeps the same key, so edits are not re-sent. That is deliberate: the
        reported bug was people getting the same story repeatedly.
      - If an article genuinely moves to a new URL we treat it as new.
    """
    canonical = _canonical_link(item.get("link")) or item.get("title", "")
    return f"{item.get('source', '')}\x00{canonical}"
