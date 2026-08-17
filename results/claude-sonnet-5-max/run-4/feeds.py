"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "dedup_key": str}

`raw_id` is whatever identifier the provider gave us, kept only for
reference/debugging (it ends up in the `items` archive table). It is
deliberately NOT what we use to decide "have we already delivered this",
because none of the three providers give us an id that is both present
and stable:

  * newsroom: `entry_id` is stable, but the item's URL carries a
    `utm_campaign` tag that changes on every crawl (it looks like a
    per-issue/per-week marker), even when the entry itself hasn't changed.
  * blogroll: has no identifier at all.
  * generic (wire): has a `guid`, but the provider mints a new one whenever
    the item is edited (typos, added tags, retitles) even though the link
    stays put.

The one thing that *is* stable across all three, once you strip tracking
parameters, is the link. So `dedup_key` is derived from the source name
plus a normalised version of the link, and that's what digest.py/store.py
use to decide whether an item has already been sent to a given channel.
See `_normalize_link` below.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query-string params that are added by tracking/analytics layers and are
# expected to change between crawls of the *same* underlying item. Strip
# these before using a link as an identity, or every crawl looks "new".
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid",
}


class FeedError(Exception):
    pass


def _normalize_link(url):
    """Canonicalise a link for identity purposes.

    Strips known tracking query params and sorts what's left so that
    harmless param-order/campaign-tag churn between fetches doesn't make
    the same story look like a new one. The link we *show* people keeps
    its original query string untouched (see `link` in the item dict);
    this normalised form is only used to build `dedup_key`.
    """
    parts = urlsplit(url)
    kept = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def _dedup_key(source, link):
    return f"{source}:{_normalize_link(link)}"


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
                "dedup_key": _dedup_key(name, r["url"]),
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
                "dedup_key": _dedup_key(name, r["permalink"]),
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
            "dedup_key": _dedup_key(name, r["link"]),
        }
        for r in doc.get("items", [])
    ]
