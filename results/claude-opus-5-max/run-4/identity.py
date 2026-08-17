"""What makes two feed entries "the same item".

This module exists because the obvious answers are all wrong, and the reason
they are wrong is not obvious. Read this before changing how dedup works.

The problem
-----------
We poll every 15 minutes. A feed holds an item for hours or days, so we see
each item ~100 times. We must send it once per channel. To do that we need a
key that is stable across polls, and the three providers each break a
different candidate key:

    provider    stable id?              stable link?          stable text?
    newsroom    yes (entry_id)          NO - utm_* rotate     yes
    blogroll    NO - no id field        yes (permalink)       NO - gets edited
    wire        NO - regenerated        yes                   NO - gets edited
                on every edit

Concretely, between fixtures/snapshot-a and fixtures/snapshot-b:

  * newsroom keeps entry_id 84121 but moves the URL from utm_campaign=w33 to
    w34. Keying on the raw link resends it.
  * blogroll retitles a post to "... (updated)" and rewrites the excerpt, at
    an unchanged permalink. Keying on a hash of the text resends it. It has
    no id to key on at all.
  * wire bumps guid "wire-...-0031" to "wire-...-0031-r2" for a copy edit, at
    an unchanged link. Keying on raw_id resends it.

So there is no single field that works everywhere. The link works everywhere
*once tracking parameters are stripped*, which is why that is the primary key
below.

The design: several keys per item, match on any
-----------------------------------------------
`keys_for()` returns every key an item could reasonably be known by. An item
counts as already-delivered if *any* of its keys has been delivered before.

That direction matters. Adding another key can only make us suppress more,
never less, because a key that fails to match simply falls through to the
others. So a key that is merely unreliable (wire's guid) is safe to include:
at worst it never helps, at best it catches an item whose URL moved. A key
that is *ambiguous* would be a real bug, which is why we never emit a key for
a missing id -- otherwise every blogroll post would share the key "id:None"
and we would silently suppress the entire feed after the first post.

Callers also record the keys of items they suppress, not just items they
send. Identities therefore accumulate aliases: if wire's guid changes on
Tuesday and its link changes on Friday, the Tuesday poll has already linked
the new guid to the known link, so Friday's item is still recognised.

The deliberate consequence: edits do not re-notify
--------------------------------------------------
Because identity is the URL and not the text, an item that is edited in place
is the same item and stays silent. That is intended -- "the same thing keeps
arriving" is the complaint we are fixing, and a typo fix is not news. If some
channel ever genuinely needs to hear about edits, that is a new feature with
its own opt-in, not a change to this key. Changing identity to include the
body text would resend every blogroll and wire copy edit to everyone.
"""

import hashlib
import urllib.parse

# Query parameters that identify the *campaign that delivered the link*
# rather than the thing being linked to. Two URLs differing only in these
# point at the same article, so they must not produce different keys.
#
# Prefix matches. `utm_*` is the Google Analytics family (utm_source,
# utm_campaign, ...); `at_*` is the BBC's equivalent.
_TRACKING_PREFIXES = ("utm_", "at_")

# Exact matches. Ad-click and mail-campaign ids.
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",       # Facebook
        "gclid",        # Google Ads
        "dclid",        # Google Display
        "msclkid",      # Microsoft Ads
        "twclid",       # Twitter
        "igshid",       # Instagram
        "mc_cid",       # Mailchimp campaign
        "mc_eid",       # Mailchimp recipient
        "_hsenc",       # HubSpot
        "_hsmi",        # HubSpot
        "cmpid",
        "campaignid",
        "campaign_id",
    }
)

# Note the omissions. A bare `ref`, `source` or `id` is stripped by some URL
# cleaners, but plenty of sites use them to select actual content, and
# dropping one would merge two genuinely different items into one key --
# which loses an item silently. When in doubt, keep the parameter: the cost
# is one duplicate, not a disappearance.


def _is_tracking(name):
    low = name.lower()
    return low.startswith(_TRACKING_PREFIXES) or low in _TRACKING_PARAMS


def normalize_url(link):
    """Reduce a link to something stable across polls.

    Every rule here is one where two spellings are the same resource:

      * scheme is dropped entirely, so a provider moving to HTTPS does not
        look like a site full of new items;
      * host is lower-cased and a default port dropped (RFC 3986 says these
        are case- and default-insensitive);
      * tracking parameters are removed and the rest sorted, because query
        parameter order carries no meaning;
      * a single trailing slash is dropped.

    The fragment is deliberately *kept*: some feeds publish several items as
    anchors into one page, and merging those would drop items.
    """
    link = (link or "").strip()
    parts = urllib.parse.urlsplit(link)
    if not parts.netloc:
        # Not a URL we understand (relative path, mailto:, urn:, junk).
        # Use it verbatim rather than mangling it into a collision.
        return link

    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None  # malformed port; ignore it rather than crash the run
    if port is not None and port not in (80, 443):
        host = f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(k)
    ]
    kept.sort()

    key = host + path
    if kept:
        key += "?" + urllib.parse.urlencode(kept)
    if parts.fragment:
        key += "#" + parts.fragment
    return key


def _content_hash(item):
    blob = "\x1f".join(
        str(item.get(f) or "") for f in ("source", "title", "summary", "published")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def keys_for(item):
    """All identities `item` may be known by, most trustworthy first.

    Never returns an empty list: an item with no key could never be
    recognised on the next poll and would be sent forever.
    """
    keys = []

    link = (item.get("link") or "").strip()
    if link:
        keys.append("url:" + normalize_url(link))

    raw_id = item.get("raw_id")
    if raw_id is not None:
        raw_id = str(raw_id).strip()
        # Namespaced by source: entry_id 84121 from newsroom must not collide
        # with a coincidental id 84121 from another provider.
        if raw_id:
            keys.append("id:{}:{}".format(item.get("source", ""), raw_id))

    if not keys:
        # No link and no id. Falling back to the text means an edit looks
        # like a new item, which is bad -- but it is strictly better than
        # having no key at all, which means resending on every single poll.
        keys.append("sha:" + _content_hash(item))

    return keys
