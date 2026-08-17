"""Feed loading.

Feeds are JSON documents. Remote feeds are fetched over HTTP; local ones
(used in development and in the fixtures/ directory) are read from disk.

The three providers we currently pull from do not agree on much, so this
module normalises them into a common item shape:

    {"title": str, "link": str, "summary": str, "published": str,
     "source": str, "raw_id": str | None, "key": str}


Item identity ("key")
---------------------
`key` answers "is this the same item I saw 15 minutes ago?". Getting it right
is the whole reason this job does not spam people, so the reasoning is written
down here rather than left to be re-derived.

Every provider mutates *something* between polls. The pair of snapshots in
fixtures/ was captured to show what:

    feed       stable across snapshots      mutates across snapshots
    --------   --------------------------   ----------------------------------
    newsroom   entry_id                     url (utm_campaign=w33 -> w34)
    blogroll   permalink                    title and excerpt (post was edited)
    wire       link                         guid (...-0031 -> ...-0031-r2)

So no single field works everywhere:

  * keying on raw_id  re-notifies every wire edit, and collapses the whole
    blogroll into one item because its raw_id is always None;
  * keying on link    re-notifies the entire newsroom feed every time the
    provider rotates its campaign parameter;
  * keying on title+summary re-notifies whenever anyone fixes a typo, which is
    exactly the blogroll case above.

Hence one rule per format, each picking the field the snapshots prove is
stable:

  newsroom  entry_id. The provider's own identifier, stable while the url
            churns. Preferred over the normalised link because it does not
            depend on _is_tracking() keeping up with whatever parameter they
            add next.
  blogroll  normalised permalink. There is no id at all, and title/summary are
            edited in place.
  generic   normalised link. The guid is regenerated on every edit, so it is
            actively harmful here despite looking like the obvious choice.

Keys are prefixed with the feed name, so identity is scoped per feed. Two feeds
carrying the same story therefore deliver twice. That is deliberate: you
subscribed to both, and collapsing across feeds would mean renaming a feed
silently suppresses its items. Drop the prefix if you ever want the opposite.
"""

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Query parameters that identify a *campaign*, not an item. Providers rotate
# these without the underlying article changing. The list is deliberately
# short: anything not listed is preserved, because plenty of sites put real
# identity in the query string (?id=123, ?p=45, ?source=reuters) and stripping
# those would merge unrelated items — a far worse failure than the duplicate
# this module exists to prevent. Only add a parameter once you have seen it
# rotate on an otherwise unchanged item.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "igshid",
        "mc_cid",
        "mc_eid",
    }
)


class FeedError(Exception):
    pass


def _is_tracking(param):
    return param in _TRACKING_PARAMS or param.startswith(_TRACKING_PREFIXES)


def normalise_link(url):
    """Strip the parts of a URL that churn without the item changing.

    Removes campaign parameters and the fragment, and lowercases the scheme and
    host (case-insensitive per RFC 3986, unlike the path). Surviving query
    parameters are sorted so that reordering does not read as a new item.
    """
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        # Malformed URL: fall back to the raw string. Still stable, which is
        # all that identity requires.
        return url.strip()

    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(k)
    ]
    return urllib.parse.urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/") or "/",
            urllib.parse.urlencode(sorted(kept)),
            "",  # a fragment never identifies an item
        )
    )


def _key(source, discriminator):
    return f"{source}:{discriminator}"


def _link_key(source, link, title):
    """Identity for feeds whose link is the trustworthy field."""
    normalised = normalise_link(link)
    if normalised:
        return _key(source, normalised)
    # No link at all. Nothing good is left, so fall back to the title —
    # deliberately not the summary, which providers edit in place. An item
    # retitled under this branch is sent again; it is the least-bad option and
    # no current feed reaches it.
    digest = hashlib.sha256((title or "").encode("utf-8")).hexdigest()[:16]
    return _key(source, f"title-{digest}")


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
        items = []
        for r in doc.get("entries", []):
            entry_id = r.get("entry_id")
            items.append(
                {
                    "title": r["headline"],
                    "link": r["url"],
                    "summary": r.get("standfirst", ""),
                    "published": r.get("published_at", ""),
                    "source": name,
                    "raw_id": None if entry_id is None else str(entry_id),
                    # entry_id is stable while the url's campaign parameter
                    # churns, so it wins when present. When absent we degrade
                    # to the link rather than dropping the row: a missed item
                    # is a worse outcome than a repeated one.
                    "key": (
                        _key(name, f"entry-{entry_id}")
                        if entry_id is not None
                        else _link_key(name, r["url"], r["headline"])
                    ),
                }
            )
        return items

    if fmt == "blogroll":
        # No stable identifier of any kind in this one, and both title and
        # excerpt get edited in place, so the permalink is all we have.
        return [
            {
                "title": r["title"],
                "link": r["permalink"],
                "summary": r.get("excerpt", ""),
                "published": r.get("date", ""),
                "source": name,
                "raw_id": None,
                "key": _link_key(name, r["permalink"], r["title"]),
            }
            for r in doc.get("posts", [])
        ]

    # "generic": has a guid, but the provider regenerates it whenever an item
    # is edited (typos, added tags, retitles) — see snapshot-a vs snapshot-b,
    # where wire-2026-08-14-0031 becomes ...-0031-r2 for the same article. The
    # guid is carried through for reference, but identity comes from the link,
    # which does stay put.
    return [
        {
            "title": r["title"],
            "link": r["link"],
            "summary": r.get("description", ""),
            "published": r.get("pubDate", ""),
            "source": name,
            "raw_id": r.get("guid"),
            "key": _link_key(name, r["link"], r["title"]),
        }
        for r in doc.get("items", [])
    ]
