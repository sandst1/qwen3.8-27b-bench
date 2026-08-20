"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "identity": str}

`identity` is the deduplication key. See `identity()` below — getting this
wrong is what makes the digest repeat itself, so read that before changing it.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class FeedError(Exception):
    pass


# Query parameters that carry campaign/analytics state rather than identifying
# the document. Providers rewrite these on every poll, so they must not be part
# of the identity key.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "fbclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
    }
)


def identity(item):
    """Return the stable dedup key for a normalised item.

    We key on the *normalised link*, deliberately not on the provider's id and
    not on the content. Each of our three feeds breaks a different obvious
    choice, which is why this looks over-thought:

      * `raw_id` (guid) is unusable. `blogroll` has no id at all, and `wire`
        regenerates its guid whenever an item is edited
        (`wire-...-0031` becomes `wire-...-0031-r2` when a comment is added).
        Keying on guid re-sends every edited wire item.
      * `title`/summary hashing is unusable. `blogroll` retitles in place
        ("Notes on port fee arithmetic" -> "... (updated)") and edits excerpts,
        so any content hash re-sends on a typo fix.
      * the *raw* link is unusable. `newsroom` stamps a fresh `utm_campaign`
        onto every url each week (`w33` -> `w34`), so the raw url changes while
        the article does not.

    Stripping tracking parameters makes the link stable across all three, and
    it is the one field every provider actually agrees identifies a document.

    The tradeoff: if a provider ever publishes two genuinely distinct items at
    one url (a "daily roundup" anchor, say), we will send only the first. That
    has not happened, and it fails safe — a missed duplicate is quieter than
    the alternative. If it does happen, give that feed its own identity rule
    here rather than switching everyone back to guid.
    """
    url = (item.get("link") or "").strip()
    if not url:
        # Nothing stable to key on. Fall back to the provider id, and failing
        # that the title, so the item still dedups against itself rather than
        # colliding with every other link-less item under a single "" key.
        return f"{item['source']}\x00{item.get('raw_id') or item.get('title', '')}"

    parts = urllib.parse.urlsplit(url)
    query = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    canonical = urllib.parse.urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            path,
            urllib.parse.urlencode(query),
            "",  # fragments never identify a feed item
        )
    )
    return canonical


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
    """Load a feed and return normalised items, each carrying its identity key."""
    items = _normalise(feed_cfg)
    for item in items:
        item["identity"] = identity(item)
    return items


def _normalise(feed_cfg):
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
