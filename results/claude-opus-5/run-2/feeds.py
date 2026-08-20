"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "dedup_key": str}


Identity (`dedup_key`) — read this before touching the formats
--------------------------------------------------------------
`dedup_key` is what stops us sending the same item every 15 minutes. It has
to stay stable across polls for an item we consider "already sent". There is
no single field that is stable across all three providers, so each format
picks its own rule and the reasoning is recorded here:

    format     raw_id across polls        link across polls       we key on
    newsroom   stable (entry_id)          rotates (utm_campaign)  raw_id
    blogroll   absent entirely            stable (permalink)      link
    generic    regenerated on every edit  stable                  link

Two traps are deliberately not obvious:

  * `newsroom` links carry a `utm_campaign` that rolls over every week, so
    the same article arrives with a different URL. Keying it on the link
    re-sends every article once a week. Its `entry_id` is genuinely stable,
    so we use that and ignore the link.

  * `generic` (the wire) has a `guid` that *looks* authoritative but the
    provider regenerates it whenever an item is edited — a typo fix turns
    `wire-...-0031` into `wire-...-0031-r2`. Keying it on the guid re-sends
    an item every time it is touched. Its link is stable, so we use that.

Titles and summaries are not usable as identity for anything: both blogroll
and the wire edit them in place (a retitle to "... (updated)", an added
paragraph), so any content hash changes while the item stays the same.

Consequence worth knowing: because we key on identity and not on content, an
item that is *edited* after we sent it is not sent again. That is deliberate
— re-announcing every typo fix is the noise we were asked to remove — but it
does mean a materially rewritten post stays silent. If that ever matters,
the place to change it is here, not in the dedup plumbing.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class FeedError(Exception):
    pass


# Query parameters that identify a campaign/referrer rather than the content.
# Stripped before a link is used as identity so that the same item arriving
# with fresh tracking params is still recognised as the same item.
_TRACKING_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_term",
                    "utm_content", "gclid", "fbclid", "mc_cid", "mc_eid")


def canonical_link(link):
    """Return `link` with tracking parameters and fragment removed.

    Used for identity only. The item keeps its original link for display, so
    readers still get whatever URL the provider wanted them to have.
    """
    if not link:
        return ""
    parts = urllib.parse.urlsplit(link)
    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), "")
    )


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


def _dedup_key(source, scheme, value):
    """Build the identity string stored in the sent ledger.

    Scoped by `source` on purpose: if two providers carry the same story we
    still deliver both, exactly as before. Cross-source de-duplication is a
    different feature (and a judgement call about which copy wins), not part
    of stopping the same feed repeating itself.

    `scheme` is part of the key so that changing a format's identity rule
    later cannot silently collide with keys written under the old rule.
    """
    return f"{source}\x1f{scheme}\x1f{value}"


def fetch(feed_cfg):
    doc = _load(feed_cfg)
    name = feed_cfg["name"]
    fmt = feed_cfg.get("format", "generic")

    if fmt == "newsroom":
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
        # entry_id is stable; the url is not (rolling utm_campaign).
        for item in items:
            item["dedup_key"] = _dedup_key(name, "raw_id", item["raw_id"])
        return items

    if fmt == "blogroll":
        # No stable identifier of any kind in this one, but the permalink
        # holds still even when the title and excerpt are edited.
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
        for item in items:
            item["dedup_key"] = _dedup_key(name, "link", canonical_link(item["link"]))
        return items

    # "generic": has a guid, but the provider regenerates it whenever an
    # item is edited (typos, added tags, retitles) — so the guid is recorded
    # for the archive but deliberately NOT used as identity. The link is
    # stable across those edits, so that is what we key on.
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
    for item in items:
        item["dedup_key"] = _dedup_key(name, "link", canonical_link(item["link"]))
    return items
