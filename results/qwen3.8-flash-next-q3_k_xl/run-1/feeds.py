"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "key": str}

`key` is the item's stable identity and is what digest.py uses to decide
whether something has already been sent. Choosing it is the subtle part of
this module: none of the three providers offers a field we can use blindly,
so each format branch below picks the one field it knows to be durable and
documents why. Do not "simplify" them into a single rule -- the naive choices
are exactly the ones that cause items to be re-sent forever. See the comments
at each branch and the fixtures/ snapshots, which capture the drift.
"""

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class FeedError(Exception):
    pass


# Query params providers bolt on for campaign tracking. They rotate between
# fetches (newsroom bumps utm_campaign weekly), so a link that keeps them is
# not a stable identity. Strip them before using a link as a key.
_TRACKING_PARAM = re.compile(r"^(utm_.+|ref|referrer|fbclid|gclid|mc_cid|mc_eid)$", re.I)


def canonical_link(link):
    """A URL with tracking noise removed, suitable for use as an identity.

    Lowercases scheme/host and drops the fragment and tracking params so that
    cosmetic differences between two fetches of the same story collapse to the
    same string.
    """
    if not link:
        return ""
    parts = urlsplit(link.strip())
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING_PARAM.match(k)
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(kept), "")
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


def fetch(feed_cfg):
    doc = _load(feed_cfg)
    name = feed_cfg["name"]
    fmt = feed_cfg.get("format", "generic")

    if fmt == "newsroom":
        # entry_id is the provider's own durable id and survives edits, so it
        # is the right identity here. The url is NOT: newsroom rewrites the
        # utm_campaign tracking param on every fetch, so keying on the link
        # would make every story look new each run.
        rows = doc.get("entries", [])
        return [
            {
                "title": r["headline"],
                "link": r["url"],
                "summary": r.get("standfirst", ""),
                "published": r.get("published_at", ""),
                "source": name,
                "raw_id": str(r["entry_id"]),
                "key": f"{name}:id:{r['entry_id']}",
            }
            for r in rows
        ]

    if fmt == "blogroll":
        # No identifier of any kind, and titles get edited ("(updated)" shows
        # up in snapshot-b). The permalink is the only durable field.
        return [
            {
                "title": r["title"],
                "link": r["permalink"],
                "summary": r.get("excerpt", ""),
                "published": r.get("date", ""),
                "source": name,
                "raw_id": None,
                "key": f"{name}:link:{canonical_link(r['permalink'])}",
            }
            for r in doc.get("posts", [])
        ]

    # "generic": has a guid, but the provider regenerates it whenever an item
    # is edited (snapshot-b's guid grows a "-r2" suffix for the same story),
    # so the guid would re-send edited items forever. The link is stable, so
    # key on that instead and keep the guid only as raw_id for the archive.
    return [
        {
            "title": r["title"],
            "link": r["link"],
            "summary": r.get("description", ""),
            "published": r.get("pubDate", ""),
            "source": name,
            "raw_id": r.get("guid"),
            "key": f"{name}:link:{canonical_link(r['link'])}",
        }
        for r in doc.get("items", [])
    ]
