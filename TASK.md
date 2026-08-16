# Task: notify-digest — stop the repeats

The prompt below is passed verbatim to the agent, with no clarification, no
follow-up, and no hints. The agent starts from a pristine copy of
`original/notify-digest/`.

---

```
We run digest.py from cron every 15 minutes. People keep getting the same
items over and over. Fix it.

Leave the codebase in a state where the next person to touch it understands
what you chose and why.
```

---

## Why this task (do not show this to the agent)

The request sounds like a one-liner — "remember what you've sent" — and a model
that treats it that way will produce something that looks finished and is
wrong within a day. The interesting decisions are all unstated:

**1. What is the identity of an item?** The three feeds disagree.
`newsroom` has a stable integer `entry_id` but its URLs carry a
`utm_campaign` that rotates weekly. `blogroll` has no identifier at all —
only a permalink and a title, and the title gets edited in place. `wire`
has a `guid` that the provider *regenerates* whenever an item is revised
(`wire-…-0031` becomes `wire-…-0031-r2` between the two fixture snapshots).
So: link-based dedupe breaks on newsroom, `raw_id`-based dedupe breaks on
wire, and content-hash dedupe re-sends every corrected typo. There is no
single field that works; the defensible answers are per-feed identity
strategies, normalised-URL-plus-source, or an explicit fallback chain — and
the choice needs to be stated, not stumbled into.

**2. Deduped for whom?** Three channels with overlapping keyword filters,
and `everything` matches all of them. Is "already sent" global (an item goes
to whoever matches first and nobody else) or per channel (each channel sees
each item once)? Per-channel is almost certainly right, and it is not what
falls out of the obvious implementation.

**3. Is an edited item a new item?** Blogroll retitles in place, wire adds a
correction. Suppressing edits loses real updates; treating them as new
reintroduces the complaint. Either answer is fine; silence is not.

**4. Ordering and crash semantics.** Sends happen inside a loop over
channels; `channels.send` raises `DeliveryError` and cron retries in fifteen
minutes. Marking sent *before* delivery loses items on failure; marking
*after* re-sends the whole batch if channel two dies after channel one
succeeded. The at-least-once / at-most-once tradeoff should be named, and
the mark should be per channel, not per run.

**5. First run and backfill.** With an empty database every historical item
is "unsent". Does the first run blast everything?

**6. `--dry-run` must not poison state**, and the suppression table grows
without bound unless something prunes it.

**Traps.** `store.items` is an append-only archive that nothing reads, and
it inserts unconditionally — a model that reuses it as the dedupe table
inherits duplicate rows on every tick unless it adds a constraint. The
docstring says nothing reads it, which is true and is also permission to
change it.
