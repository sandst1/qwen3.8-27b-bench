"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "key": str}

`key` is the item's stable identity, used to decide whether we have already
delivered it. See "Item identity" below; it is the reason this module knows
about each provider's quirks instead of digest.py guessing.

## Item identity

There is no single field that identifies an item across all three providers.
Each one breaks a different obvious choice, which is why the naive answers
("use the guid", "use the link") all resend things:

    provider   stable                unstable
    ---------  --------------------  ------------------------------------
    newsroom   entry_id              url — the utm_campaign parameter is
                                     rotated weekly (…w33 → …w34)
    blogroll   permalink             title and excerpt are edited in place;
                                     there is no id field at all
    generic    link                  guid — regenerated on every edit
                                     ("wire-…-0031" → "wire-…-0031-r2")

So the rule is per format:

  * Trust the provider's id only where the provider actually keeps it stable
    (newsroom). For `generic` we deliberately ignore `guid` even though it is
    present, because it is a revision id, not an item id.
  * Otherwise fall back to the link with tracking parameters stripped.

Title and summary are never part of the identity: both blogroll and generic
edit them in place after publication, and treating an edit as a new item is
exactly the bug we are avoiding. The flip side is that a genuine correction
will not be re-sent to a channel that already saw the original — that is the
intended trade, since these are digests, not alerts.
"""

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class FeedError(Exception):
    pass


# Tracking parameters that providers rotate without the item itself changing.
# Stripped before a link is used as an identity.
_TRACKING_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_term",
                    "utm_content", "gclid", "fbclid", "ref", "ref_src")


def normalise_link(link):
    """Strip rotating tracking parameters so a link can serve as an identity.

    newsroom re-stamps every url with the current week's utm_campaign, so the
    raw url changes weekly for items that have not changed at all.
    """
    if not link:
        return ""
    parts = urllib.parse.urlsplit(link)
    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query = urllib.parse.urlencode(kept)
    # Also drop the fragment: it is a scroll position, not an identity.
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def item_key(source, raw_id, link, title, trust_raw_id):
    """Build the stable identity for an item.

    Namespaced by source so two providers carrying the same story stay
    distinct items (channels expect to see both, attributed).
    """
    if trust_raw_id and raw_id:
        return f"{source}:id:{raw_id}"
    normalised = normalise_link(link)
    if normalised:
        return f"{source}:link:{normalised}"
    # An item with no id and no link should not exist, but if a provider ever
    # emits one, hashing the title keeps the run self-consistent rather than
    # collapsing every such item into a single key and dropping all but one.
    digest = hashlib.sha256((title or "").encode()).hexdigest()[:16]
    return f"{source}:title:{digest}"


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
        # entry_id is a real item id and survives edits; the url does not
        # (utm_campaign rotates weekly), so identity comes from entry_id.
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
        return _with_keys(items, name, trust_raw_id=True)

    if fmt == "blogroll":
        # No stable identifier of any kind in this one, and titles/excerpts
        # are edited in place — so the permalink is the only usable identity.
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
        return _with_keys(items, name, trust_raw_id=False)

    # "generic": has a guid, but the provider regenerates it whenever an
    # item is edited (typos, added tags, retitles) — so it is a revision id,
    # not an item id. We keep it for the archive but identify on the link.
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
    return _with_keys(items, name, trust_raw_id=False)


def _with_keys(items, source, trust_raw_id):
    for item in items:
        item["key"] = item_key(
            source, item.get("raw_id"), item.get("link"), item.get("title"),
            trust_raw_id,
        )
    return items
