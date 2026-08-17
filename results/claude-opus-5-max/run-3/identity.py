"""Item identity: deciding when two feed entries are the same thing.

Everything here answers one question: have we already told this reader about
this item?  Get it wrong one way and people are re-notified every 15 minutes
(the bug this module was written for).  Get it wrong the other way and news
is silently swallowed, which is worse and much harder to notice.  So the
rule below is biased towards a *stable* key, and it never lets a key
collapse to something empty or accidentally shared.

Why not the obvious candidates
------------------------------
There is no single raw field that works, because each provider breaks a
different one (the quirks are described in feeds.py, and both fixture
snapshots demonstrate them):

    provider   raw_id                        link
    newsroom   stable entry_id               churns: ?utm_campaign=w33 -> w34
    blogroll   absent, always None           stable permalink
    wire       churns: "-r2" on every edit   stable

  * raw_id   works for newsroom alone.  wire regenerates its guid whenever
             an editor touches an item, and blogroll has no id at all.
  * link     works for blogroll and wire, and fails for newsroom only
             because of a weekly campaign parameter.
  * title,   or any hash of the content: blogroll retitled a post to
    content  "... (updated)" and both wire and blogroll edited body text
             between snapshots, so a content hash re-fires on every typo
             fix.

Canonicalising the link repairs the single case link gets wrong, which
makes it work for all three.  So the key is the link with tracking noise
stripped.

raw_id is deliberately *not* consulted even where it happens to be good
(newsroom), because "use raw_id for this provider, the link for that one"
is a per-provider rule, and a per-provider rule is one the next provider
gets wrong silently.  One uniform rule fails visibly and in one place.

Two consequences worth knowing
------------------------------
* An edit is not a new item.  A corrected table or an added paragraph
  reuses the key and is not re-sent.  That is the intended product
  behaviour: readers complained about repeats, not about missing errata.
* The link namespace is global rather than per feed, so if two providers
  carry the identical URL the story is sent once, not twice.  raw_id
  fallbacks *are* scoped per feed, since raw ids are only unique within a
  provider.
"""

import hashlib
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

# Query parameters that identify the *campaign that delivered* the link
# rather than the thing being linked to.  Stripping them is what makes the
# newsroom feed stable.  Kept deliberately short: anything not listed here
# is preserved, because plenty of sites really do identify content with a
# query parameter (?id=, ?p=, ?story=) and dropping one of those would
# merge unrelated items and silently suppress them.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",      # Facebook
        "gclid",       # Google Ads
        "dclid",       # Google Display
        "msclkid",     # Microsoft Ads
        "igshid",      # Instagram
        "mc_cid",      # Mailchimp campaign
        "mc_eid",      # Mailchimp recipient
        "_hsenc",      # HubSpot
        "_hsmi",       # HubSpot
    }
)


def _is_tracking(name):
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES)


def canonical_link(url):
    """Strip the parts of `url` that vary between polls of the same item.

    Conservative on purpose: it only removes things that cannot change
    which document is being addressed.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        # Not parseable as a URL.  Better to key on the raw string than to
        # guess; a weird-but-constant string still dedupes correctly.
        return url.strip()

    # Scheme and host are case insensitive (RFC 3986); the path is not, so
    # it is left alone.
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()

    # "/a/" and "/a" are the same document in every feed we have seen, and
    # normalising costs nothing while saving a full re-notify storm if a
    # provider ever flips its trailing-slash style during a CMS migration.
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    # Sorted so that a provider reordering its parameters is not read as a
    # different item.
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(key)
    ]
    query = urlencode(sorted(kept))

    # A "#section" anchor points into the same document, so it is dropped.
    # A "#/route" or "#!/route" fragment is hash based routing, where the
    # fragment is the only thing distinguishing two items, so it is kept.
    fragment = parts.fragment
    if not fragment.startswith(("/", "!")):
        fragment = ""

    return urlunsplit((scheme, netloc, path, query, fragment))


def item_key(item):
    """Return the stable dedup key for a normalised feed item.

    The prefixes keep the three strategies in separate namespaces, so a
    link can never collide with a raw id that happens to look like it.
    """
    link = (item.get("link") or "").strip()
    if link:
        return "link:" + canonical_link(link)

    # No link.  Nothing in the current feeds hits this, but an empty key
    # would merge every link-less item into one and suppress the lot, so
    # fall back rather than trust the caller.
    raw_id = (item.get("raw_id") or "").strip()
    if raw_id:
        return "raw:{}:{}".format(item.get("source", ""), raw_id)

    # Neither a link nor an id.  A content hash re-fires when the item is
    # edited, which is the wrong behaviour, but re-sending an occasional
    # edit beats never sending the item at all.
    material = "\n".join(
        (
            item.get("source") or "",
            item.get("title") or "",
            item.get("published") or "",
        )
    )
    return "sha:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
