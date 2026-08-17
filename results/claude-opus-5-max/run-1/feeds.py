"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "identity": str}


Item identity
-------------
`identity` answers "is this the thing we already sent?". Everything
downstream (the dedup ledger, the archive) keys off it, so it is chosen here,
per format, right next to the provider quirk that motivates the choice.

There is no single field that works for all three providers -- each one
breaks a different obvious answer:

    newsroom   `entry_id` is stable across polls, so use it.
               NOT the link: the provider rotates `utm_campaign` on its URLs
               every week (w33 -> w34 between the two fixtures), so keying on
               the raw link re-notifies the whole feed once a week.

    blogroll   No identifier of any kind, so use the normalised permalink,
               which is stable.
               NOT the title or summary: posts are edited in place (the
               fixtures retitle one to "... (updated)" and rewrite its
               excerpt), and a typo fix must not re-notify.

    generic    `guid` exists but the provider regenerates it on every edit
               ("wire-...-0031" -> "wire-...-0031-r2" between the fixtures),
               so it is worthless for identity. The link is stable, so use
               the normalised link and ignore the guid.

The rule of thumb when adding a fourth provider: prefer a publisher-assigned
id *only* if it is stable under edits; otherwise fall back to the normalised
link. Never fold title/summary into identity -- that turns every typo fix
into a re-notification, which is the exact complaint this scheme exists to
stop.

Identities are stored as readable, prefixed strings ("id:84121",
"url:https://...") rather than opaque hashes, so that a human debugging
"why did this not go out?" can read the ledger directly:

    sqlite3 digest.sqlite3 'SELECT * FROM sent ORDER BY sent_at DESC LIMIT 20;'
"""

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class FeedError(Exception):
    pass


# Query parameters that identify a *campaign*, not a document. Providers
# rotate these, so they must not contribute to identity. Kept deliberately
# short: anything not listed here is treated as meaningful, because wrongly
# stripping a real parameter merges two distinct items and silently drops a
# notification, which is worse than the duplicate it would save.
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "ref_src",
    }
)
_TRACKING_PREFIXES = ("utm_",)


def _is_tracking(param):
    return param in _TRACKING_PARAMS or param.startswith(_TRACKING_PREFIXES)


def normalise_url(url):
    """Reduce a URL to the document it points at.

    Strips campaign parameters and the fragment, lowercases the parts that
    are case-insensitive by spec (scheme and host, but *not* the path), and
    sorts the surviving query so that parameter reordering does not read as
    a different item.

    Anything unparseable is returned stripped-but-otherwise-untouched, on
    the same "never silently merge" principle as _TRACKING_PARAMS.
    """
    if not url:
        return ""
    url = url.strip()
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url

    if not parts.netloc:
        # Relative or malformed; nothing safe to normalise.
        return url

    host = parts.hostname or ""
    if parts.port is not None:
        default_port = {"http": 80, "https": 443}.get(parts.scheme.lower())
        if parts.port != default_port:
            host = f"{host}:{parts.port}"

    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = sorted((k, v) for k, v in query if not _is_tracking(k))

    path = parts.path
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")

    return urllib.parse.urlunsplit(
        (
            parts.scheme.lower(),
            host,
            path,
            urllib.parse.urlencode(query),
            "",  # fragment: client-side anchor, same document
        )
    )


def _identity(kind, value, source, title, published):
    """Build an identity string, degrading safely if the basis is missing.

    A blank basis (a feed item with no id and no link) must not collapse
    every such item onto one key -- that would silently swallow real
    notifications. We fall back to a content digest instead: it re-notifies
    if the item is later edited, which is noisy but visible. Duplicate over
    drop, every time.
    """
    if value:
        return f"{kind}:{value}"
    material = "\x1f".join((source, title or "", published or ""))
    return "hash:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


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
        items = []
        for r in rows:
            raw_id = str(r["entry_id"])
            items.append(
                {
                    "title": r["headline"],
                    "link": r["url"],
                    "summary": r.get("standfirst", ""),
                    "published": r.get("published_at", ""),
                    "source": name,
                    "raw_id": raw_id,
                    # entry_id survives edits and the weekly utm_campaign
                    # rotation on r["url"], so it is the trustworthy key here.
                    "identity": _identity(
                        "id", raw_id, name, r["headline"], r.get("published_at", "")
                    ),
                }
            )
        return items

    if fmt == "blogroll":
        # No stable identifier of any kind in this one.
        items = []
        for r in doc.get("posts", []):
            items.append(
                {
                    "title": r["title"],
                    "link": r["permalink"],
                    "summary": r.get("excerpt", ""),
                    "published": r.get("date", ""),
                    "source": name,
                    "raw_id": None,
                    # Permalink is all we have, and it is stable even when the
                    # post is retitled/re-edited -- which it is, routinely.
                    "identity": _identity(
                        "url",
                        normalise_url(r["permalink"]),
                        name,
                        r["title"],
                        r.get("date", ""),
                    ),
                }
            )
        return items

    # "generic": has a guid, but the provider regenerates it whenever an
    # item is edited (typos, added tags, retitles).
    items = []
    for r in doc.get("items", []):
        items.append(
            {
                "title": r["title"],
                "link": r["link"],
                "summary": r.get("description", ""),
                "published": r.get("pubDate", ""),
                "source": name,
                # Kept for the archive so we can see what the provider claimed,
                # but deliberately NOT used for identity: see module docstring.
                "raw_id": r.get("guid"),
                "identity": _identity(
                    "url",
                    normalise_url(r.get("link", "")),
                    name,
                    r["title"],
                    r.get("pubDate", ""),
                ),
            }
        )
    return items
