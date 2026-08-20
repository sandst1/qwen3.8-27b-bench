"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "dedupe_key": str}

`dedupe_key` is what stops us re-sending the same item every 15 minutes, so
it is worth explaining. It has to be *stable across polls* for an item that
has not changed, and it has to be *different* for two genuinely different
items. Each of our three providers violates a different obvious choice:

    provider    raw_id                  link                    title
    newsroom    stable                  rotating utm_campaign   stable
    blogroll    absent entirely         stable                  edited in place
    generic     regenerated on edit     stable                  stable

So there is no single field that works everywhere. "Always use the guid"
spams from blogroll and generic; "always use the link" spams from newsroom;
"hash the content" spams from blogroll and generic. The key is therefore
chosen per format, here, next to the format quirks it has to work around.

Keys are only ever compared within one source (see store.py), because an
id-derived key like "84121" is only meaningful inside the feed that issued it.
"""

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class FeedError(Exception):
    pass


# Query parameters that identify the *campaign that delivered* a link rather
# than the thing it points at. Newsroom rotates utm_campaign every week, which
# is what made link-only dedupe resend its whole feed each week.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "utm_reader", "utm_name", "fbclid", "gclid", "dclid",
        "msclkid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src", "referrer",
        "campaign", "source",
    }
)


def normalise_link(link):
    """Reduce a URL to something stable enough to identify an item by.

    Strips tracking parameters, the fragment, default ports and a trailing
    slash, and lowercases the parts of a URL that are case-insensitive. The
    path is left alone: it is case-sensitive on plenty of servers.

    Non-tracking query parameters are kept and sorted -- they often *are* the
    identity (`?id=123`), so dropping the query wholesale would collapse
    distinct items together, which is worse than a duplicate.
    """
    if not link:
        return ""
    parts = urllib.parse.urlsplit(link.strip())
    if not parts.scheme and not parts.netloc:
        # Not a URL we can reason about; use it verbatim rather than mangle it.
        return link.strip()

    host = parts.hostname or ""
    if parts.port and not (
        (parts.scheme == "http" and parts.port == 80)
        or (parts.scheme == "https" and parts.port == 443)
    ):
        host = f"{host}:{parts.port}"

    query = urllib.parse.urlencode(
        sorted(
            (k, v)
            for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS
        )
    )

    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parts.scheme.lower(), host, path, query, ""))


def _content_key(item):
    """Last-resort key for an item with neither a usable id nor a link.

    None of our current feeds reach this. It keys on title+timestamp, which
    means an in-place edit would look like a new item and get re-sent once --
    acceptable only because the alternative is no key at all.
    """
    digest = hashlib.sha256(
        "\x00".join([item.get("title", ""), item.get("published", "")]).encode()
    ).hexdigest()
    return "content:" + digest[:32]


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
        items = [
            {
                "title": r["headline"],
                "link": r["url"],
                "summary": r.get("standfirst", ""),
                "published": r.get("published_at", ""),
                "source": name,
                "raw_id": str(r["entry_id"]),
                # entry_id is a stable primary key on the provider's side and
                # survives retitles and edits. Deliberately NOT the URL: the
                # utm_campaign on it rotates weekly, so a link-derived key
                # would re-send this entire feed every week.
                "dedupe_key": "id:" + str(r["entry_id"]),
            }
            for r in rows
        ]
        return items

    if fmt == "blogroll":
        # No stable identifier of any kind in this one.
        return [
            _with_link_key(
                {
                    "title": r["title"],
                    "link": r["permalink"],
                    "summary": r.get("excerpt", ""),
                    "published": r.get("date", ""),
                    "source": name,
                    "raw_id": None,
                }
            )
            # The permalink is the only stable thing here. Titles and excerpts
            # are edited in place ("... (updated)"), so anything derived from
            # the item's text would re-send the post on every edit.
            for r in doc.get("posts", [])
        ]

    # "generic": has a guid, but the provider regenerates it whenever an
    # item is edited (typos, added tags, retitles). That makes the guid
    # useless for dedupe -- it is exactly what produced the repeats -- so we
    # key on the link, which stays put across those edits.
    return [
        _with_link_key(
            {
                "title": r["title"],
                "link": r["link"],
                "summary": r.get("description", ""),
                "published": r.get("pubDate", ""),
                "source": name,
                "raw_id": r.get("guid"),
            }
        )
        for r in doc.get("items", [])
    ]


def _with_link_key(item):
    """Key an item by its normalised link, falling back to its content."""
    link = normalise_link(item.get("link", ""))
    item["dedupe_key"] = "link:" + link if link else _content_key(item)
    return item
