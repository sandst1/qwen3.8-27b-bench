"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "dedup_key": str}

Identity (dedup_key)
--------------------
"Have we already sent this item?" is the whole ballgame for a cron job, and
none of the providers give us a field we can trust on its own. The fixtures/
snapshots (same feeds captured at two times) show exactly how each one lies:

  * newsroom  entry_id is stable, but the URL carries a per-campaign
              utm_campaign that changes week to week (w33 -> w34).
  * wire      the link is stable, but guid is regenerated whenever an item is
              edited ("...-0031" -> "...-0031-r2").
  * blogroll  the permalink is stable, but there is no id at all and the title
              gets edited ("... (updated)").

So raw_id is wrong for two of three feeds and the raw link is wrong for
newsroom. The one signal that stays put across every provider is the link
once we drop tracking parameters and the fragment. We build the key from
that, scoped to the source so two feeds pointing at the same URL stay
distinct. If a future feed publishes a genuinely stable id, prefer it in
item_key() — but only a *stable* one; a regenerating guid is worse than the
link.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class FeedError(Exception):
    pass


# Query params that are appended for ad-campaign / traffic tracking and are
# not part of the article's identity. Matched by prefix (case-insensitive).
TRACKING_PARAM_PREFIXES = ("utm_", "mc_", "pk_")


def canonical_link(link):
    """Strip tracking params and the fragment so e.g. the same newsroom story
    with utm_campaign=w33 and w34 collapses to one URL."""
    parts = urlsplit(link)
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(TRACKING_PARAM_PREFIXES)
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(kept), "")
    )


def item_key(item):
    """Stable identity for an item. See the module docstring for why this is
    the canonical link and not raw_id."""
    return f"{item['source']}|{canonical_link(item['link'])}"


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
        items = [
            {
                "title": r["headline"],
                "link": r["url"],
                "summary": r.get("standfirst", ""),
                "published": r.get("published_at", ""),
                "source": name,
                "raw_id": str(r["entry_id"]),
            }
            for r in doc.get("entries", [])
        ]
    elif fmt == "blogroll":
        # No stable identifier of any kind in this one.
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
    else:
        # "generic": has a guid, but the provider regenerates it whenever an
        # item is edited (typos, added tags, retitles), so we don't key on it.
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
        item["dedup_key"] = item_key(item)
    return items
