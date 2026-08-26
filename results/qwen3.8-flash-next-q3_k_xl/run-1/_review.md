# Review — notify-digest dedupe fix

## 1. Summary

The agent added a per-channel `deliveries` table and a per-feed `key` field
(`feeds.py`), then filtered each channel's candidate list against
`already_delivered` before sending and wrote `mark_delivered` only after
`channels.send()` returned without raising; it left `render.py`,
`channels.py`, and `config.example.toml` untouched and documented the
identity choices in both `feeds.py`/`store.py` docstrings and a new README
section. I re-ran the tool against `fixtures/snapshot-a` then
`fixtures/snapshot-b` with a real sqlite db and confirmed all three feeds'
specific drift cases (rotating `utm_campaign`, a regenerated `guid`, and a
title-only edit with no id at all) are correctly deduped while the one
genuinely new story still gets delivered. I would merge this with one
required fix: the pre-existing unconditional `INSERT` into the `items`
archive table (`store.py:51-60`) is untouched and now runs on every 15-minute
tick forever, which is the literal "unconditional insert" scenario the task
should have dealt with.

## 2. Per-category scoring

### Identity strategy — 10 / 10

`feeds.py` picks a different durable field per provider and states the
failure mode for the field it rejects, right at the point of choice:

```python
if fmt == "newsroom":
    # entry_id is the provider's own durable id and survives edits, so it
    # is the right identity here. The url is NOT: newsroom rewrites the
    # utm_campaign tracking param on every fetch, so keying on the link
    # would make every story look new each run.
    ...
    "key": f"{name}:id:{r['entry_id']}",
```

```python
    # "generic": has a guid, but the provider regenerates it whenever an item
    # is edited (snapshot-b's guid grows a "-r2" suffix for the same story),
    # so the guid would re-send edited items forever. The link is stable, so
    # key on that instead and keep the guid only as raw_id for the archive.
    ...
    "key": f"{name}:link:{canonical_link(r['link'])}",
```

```python
    if fmt == "blogroll":
        # No identifier of any kind, and titles get edited ("(updated)" shows
        # up in snapshot-b). The permalink is the only durable field.
        ...
        "key": f"{name}:link:{canonical_link(r['permalink'])}",
```

I verified all three against the actual fixtures rather than trusting the
comments. Ran snapshot-a for real, then snapshot-a again (nothing new),
then snapshot-b:

```
$ python3 digest.py --config config-a.toml --db digest.sqlite3   # first run: 12 sent
$ python3 digest.py --config config-a.toml --db digest.sqlite3   # same snapshot again
sent 0 items
$ python3 digest.py --config config-b.toml --db digest.sqlite3   # snapshot-b
=== ops ===
Ops digest — 1 item(s)
* Union responds to port fee inquiry  [newsroom]
sent 2 items
```

Only the genuinely new newsroom story (`entry_id 84130`) went out. The
snapshot-b edits that are designed to break naive dedupe were all correctly
suppressed:

- newsroom `entry_id 84121`, same story, `utm_campaign=w33` → `w34` in the
  link — suppressed (keyed on `entry_id`, not link).
- wire `guid wire-2026-08-14-0031` → `...-0031-r2`, same `link` — suppressed
  (keyed on link, not guid).
- blogroll "Notes on port fee arithmetic" → "... (updated)", same
  `permalink`, no id at all — suppressed (keyed on link, no id needed).

`canonical_link()` also strips a small set of tracking-param names via regex
before using a link as a key, which is defensive engineering beyond what the
fixtures require (none of the fixture links that use `canonical_link` — wire,
blogroll — actually carry tracking params; only newsroom's link has
`utm_campaign`, and newsroom doesn't key on its link at all). It's not wrong,
just untested by anything in this repo. That's a code-quality note, not an
identity-strategy deduction — the three fixture-mandated cases are all
handled and verified.

### Ambiguity handling — 6 / 8

Two of the three canonical forks are named and resolved explicitly in both
code comments and the README:

- **Per-channel vs. global suppression** — resolved per-channel, and stated:
  `store.py:9-11` — *"`deliveries` ... records one row per (channel, item
  key) that we have successfully handed to a channel"* — and in the README:
  *"Tracking is per channel: two channels can each deliver the same item
  once, but neither will repeat it."* I confirmed this behaviorally: `ops`
  and `energy` and `everything` all received the same first-run items
  independently (three separate `=== channel ===` blocks with overlapping
  stories), and each channel's suppression is tracked separately in
  `deliveries` (verified via `SELECT * FROM deliveries` after a run).
- **Edited item as new vs. same** — resolved as "same" and stated, with the
  specific fixture evidence quoted in the README (see identity section
  above).

The third fork — **first-run backfill** — is not named anywhere. `store.py`'s
`deliveries` table starts empty, so the very first invocation against a
feed with existing history sends everything currently live, exactly once,
with no discussion of whether that's desired. I ran a fresh db against
snapshot-a and got `sent 12 items` on the very first tick — for a real
feed with months of backlog this could be a large one-time blast to every
channel the moment this changes ships, and nothing in the README warns an
operator about it or offers a way to seed `deliveries` from the existing
`items` archive to avoid it. Sending everything on cold start is a
defensible default, but it is a real decision the deployer needs to know
about, and the agent decided it silently.

Per rubric: two forks named and resolved, one decided silently (arguably
correctly, but undocumented) → mid-to-high band, not top.

### Failure-mode reasoning — 7 / 8

Per-channel marking, and it's ordered correctly for an at-least-once
guarantee — mark only after send succeeds:

```python
channels.send(chan_cfg, body)
# Mark only after the channel accepted the digest. If send() raised we
# leave the items unmarked so the next cron tick retries them.
store.mark_delivered(db, chan_cfg["name"], [i["key"] for i in selected])
```

I confirmed this with a real partial-failure run: two channels, `ops`
(stdout, always succeeds) and `broken` (webhook to a closed port). First
run: `ops` prints its digest and gets marked delivered; `broken` raises
`DeliveryError` and the process exits 1 without ever calling
`mark_delivered` for it (confirmed via `SELECT * FROM deliveries` — only
`ops` rows exist). Second run against the same db and feed: `ops` prints
**nothing** (correctly not re-sent) while `broken` is retried and fails
again. That is exactly "per-channel marking, correct ordering, partial
failure doesn't lose or duplicate a whole batch."

Where it loses half a band: the guarantee is asserted more strongly than it
actually is. `digest.py`'s module docstring says:

```
Each channel receives every matching item exactly once: run_once consults the
deliveries table (store.py) before sending and records what it sent afterwards,
so a given item is never delivered to the same channel twice.
```

That's at-least-once, not exactly-once: `channels.send()` for a webhook can
return successfully, and then the process can die (OOM, SIGKILL, box reboot)
before `store.mark_delivered`'s `conn.commit()` runs. On the next tick the
same item would be re-sent. The actual tradeoff — retry-on-uncertainty over
silent loss — is the right one and is implicitly argued via the code
comment ("If send() raised we leave the items unmarked so the next cron
tick retries them"), but the docstring's "exactly once" phrasing overstates
what was built and isn't corrected anywhere else in the docs.

### Existing-code respect — 4 / 6

`render.py`, `channels.py`, and `config.example.toml` are untouched;
`feeds.py` and `store.py` are extended in place rather than rewritten;
`--dry-run` still works and, confirmed by test, doesn't persist (`digest2.sqlite3`,
run twice with `--dry-run`, printed the identical "would send" output both
times). No scope creep — no plugin system, no scheduler, nothing beyond the
dedupe mechanism the task asked for.

The rubric calls out one specific defect by name: *"Reusing the `items`
archive is fine if the unconditional insert is dealt with."* It wasn't. This
line is byte-for-byte the original, untouched:

```python
def record_items(conn, source, items):
    conn.executemany(
        "INSERT INTO items (source, raw_id, title, link, summary, published)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [...],
    )
    conn.commit()
```

`digest.py:40` still calls this unconditionally for every item on every
fetch, whether or not it's new. I ran it three times (snapshot-a,
snapshot-a again with nothing new, snapshot-b) and checked the table:

```
$ sqlite3 digest.sqlite3 "SELECT source, raw_id, title, COUNT(*) FROM items GROUP BY source, raw_id, title ORDER BY COUNT(*) DESC LIMIT 3;"
blogroll||A short history of offshore tenders|3
newsroom|84118|Grid operator delays offshore tender|3
newsroom|84121|Regulator opens inquiry into port fees|3
```

Three ticks, most stories already inserted three times. On a real 15-minute
cron with a feed of 20 static items, that's ~1,900 duplicate archive rows
per feed per day, forever, with no unique constraint and no discussion of it
anywhere in the new docs. This is precisely the failure mode the rubric
names, and it went unaddressed — no dedup on insert, no comment
acknowledging the tradeoff, no mention in the README's otherwise-thorough
dedupe section. That costs real points here even though everything else
about how the agent worked with the existing code was clean.

### Code quality — 4 / 4

Readable, no dead code, sensible SQL. The new schema is a genuine additive
migration, not a rewrite — `deliveries` is a new `CREATE TABLE IF NOT
EXISTS`, and I verified it against a database built from the *original*
`SCHEMA` (pre-existing `items` table, no `deliveries`): running the new
`digest.py` against it added the `deliveries` table and worked correctly
without any manual migration step. `PRIMARY KEY (channel, item_key)` plus
`INSERT OR IGNORE` in `mark_delivered` (`store.py:79-89`) is exactly the
right primitive for "record once, safe to re-run."

### Documentation — 2.5 / 4

The README gets a dedicated section, well past a docstring, with concrete
per-feed rationale tied to the actual fixture drift:

```
- **newsroom** — keys on `entry_id`. The URL is *not* stable: `utm_campaign`
  is rewritten on every fetch, so keying on the link re-sends every story.
- **wire (generic)** — keys on the link. The `guid` is *not* stable: it is
  regenerated whenever an item is edited (`...0031` → `...0031-r2` in
  snapshot-b), so keying on the guid re-sends edited stories.
- **blogroll** — keys on the permalink. There is no id at all and titles get
  edited, so the permalink is the only durable field.
```

But the rubric asks specifically for three things and only one is fully
covered: dedupe behavior (covered, well), how to reset it (**not
mentioned anywhere** — there's no documented way to force a resend, e.g.
after a filter-rule bug sent nothing that should have gone out; an operator
would have to know to go delete rows from `deliveries` in the sqlite file
directly, unassisted), and what happens on first run (**not mentioned** —
see the ambiguity-handling section; a fresh `deliveries` table means the
very first tick sends the entire live feed history to every channel, and
nothing warns a deployer of that before they flip this on in production).

## 3. What it missed

- **First-run backfill is undocumented and undiscussed** (see Ambiguity
  handling). A fresh deploy against a feed with real history will blast
  every channel with everything currently live on the very first tick.
- **No reset mechanism** is documented or provided (no `--reset`, no
  README note about `DELETE FROM deliveries`, nothing).
- **The `items` archive's unconditional insert is untouched** and now
  accumulates duplicate rows on every single tick forever — the exact
  defect the rubric calls out by name for this category.
- **The "exactly once" claim in `digest.py`'s docstring overstates the
  guarantee** actually implemented (which is at-least-once, biased toward
  retry over loss — the right choice, just not accurately labeled).
- No consideration of concurrent/overlapping cron runs (e.g. a slow run
  still executing when the next `*/15` tick fires) — not required by the
  prompt, but also not mentioned as an assumption.

## 4. Bugs

I did not find anything that crashes or corrupts data under normal use — the
core dedupe path, the per-channel partial-failure path, `--dry-run`, and the
schema migration onto a pre-existing database all worked as documented when
I exercised them directly. The closest thing to a "bug" is the archive-table
growth issue above, which isn't a crash but is a real, unbounded resource
leak in production that will eventually matter (disk usage, `VACUUM` cost,
slow `count_items()` if anyone starts using it) and is squarely what the
rubric's existing-code-respect category is checking for.

Reproduction for the archive growth (not a crash, but a genuine defect):

```sh
cd /tmp/scratch-copy   # copy of the agent's tree
rm -f digest.sqlite3
python3 digest.py --config config-a.toml --db digest.sqlite3   # tick 1
python3 digest.py --config config-a.toml --db digest.sqlite3   # tick 2, same feed
sqlite3 digest.sqlite3 "SELECT COUNT(*) FROM items;"            # 12, not 6
```

```json
{
  "identity_strategy": 10,
  "ambiguity_handling": 6,
  "failure_mode_reasoning": 7,
  "existing_code_respect": 4,
  "code_quality": 4,
  "documentation": 2.5,
  "total": 33.5,
  "would_merge": "yes-with-fixes",
  "one_line": "Excellent per-feed identity and per-channel retry; ignores the archive's unconditional-insert growth."
}
```
