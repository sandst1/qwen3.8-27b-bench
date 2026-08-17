"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "dedupe_key": str}

`raw_id` is kept only as a provider-supplied breadcrumb for the archive
table; nothing should use it to decide whether an item is new. It is
either absent (`blogroll` has no identifier at all) or unreliable (the
`generic` provider mints a fresh `guid` every time an item is edited -
typo fixes, added tags, retitles - so "new guid" does not mean "new
story"). `dedupe_key` is what callers should actually key delivery state
on: it is derived from the link, which is the one thing all three
providers keep pointing at the same story once you strip crawl-to-crawl
tracking noise (see `_normalize_link`). This is computed once here,
alongside the rest of the provider-specific knowledge, so nothing
downstream needs to know which quirks belong to which feed format.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Query params that vary between crawls of the *same* story (campaign
# tags, share tokens, ...) and therefore must not affect identity.
_TRACKING_PARAM_PREFIXES = ("utm_",)


class FeedError(Exception):
    pass


def _normalize_link(url):
    """Canonicalise a link for use as a dedupe key.

    We strip known tracking query params and the fragment, but otherwise
    leave the query string alone: for some feeds the query string *is*
    part of the identity (e.g. `?id=123`), so blanket-stripping everything
    would risk silently merging two unrelated items instead of just
    ignoring tracking noise.
    """
    parts = urllib.parse.urlsplit(url)
    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PARAM_PREFIXES)
    ]
    query = urllib.parse.urlencode(kept)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


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
                "dedupe_key": f"{name}:{_normalize_link(r['url'])}",
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
                "dedupe_key": f"{name}:{_normalize_link(r['permalink'])}",
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
            "dedupe_key": f"{name}:{_normalize_link(r['link'])}",
        }
        for r in doc.get("items", [])
    ]
