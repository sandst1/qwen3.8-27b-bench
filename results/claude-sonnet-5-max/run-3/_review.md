# Review: claude-sonnet-5-max, run-3

## 1. Summary

The agent introduced a per-item `dedupe_key` in `feeds.py` (feed-namespaced,
tracking-param-stripped link) and a per-channel `deliveries` ledger in
`store.py`, wired through `digest.py` so delivery is only recorded after
`channels.send()` returns successfully. It left `channels.py` and `render.py`
untouched, kept the existing `items` archive table and its unconditional
insert exactly as before, and wrote a substantial README section explaining
identity, per-channel suppression, first-run backfill, and the failure
ordering. I would merge this: it is minimal, correctly reasoned, and verified
against the fixtures to actually suppress the three duplicate cases (rotated
`utm_campaign`, missing `raw_id`, regenerated `guid`) while still delivering
the genuinely new item in snapshot-b.

## 2. Per-category scoring

### Identity strategy — 9.5 / 10

`feeds.py` computes `dedupe_key = f"{name}:{_normalize_link(link)}"` for all
three formats, explicitly *not* using `raw_id`:

```python
def _normalize_link(url):
    parts = urllib.parse.urlsplit(url)
    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PARAM_PREFIXES)
    ]
    query = urllib.parse.urlencode(kept)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
```

I ran it against both fixture snapshots with the same `--db`. First pass on
snapshot-a sent 12 items across three channels; a second pass on the same
snapshot-a sent 0; switching to snapshot-b sent exactly 2 — the genuinely new
newsroom story, and correctly *not* the re-guid'd wire story
(`wire-2026-08-14-0031` → `-r2`, same link), not the edited blog post (same
`permalink`), and not the newsroom item whose only change was
`utm_campaign=w33` → `w34`:

```
$ python3 -c "import feeds; print(feeds._normalize_link('...utm_campaign=w33')); print(feeds._normalize_link('...utm_campaign=w34'))"
https://newsroom.example/2026/08/port-fees
https://newsroom.example/2026/08/port-fees
```

The strategy is stated with its own failure mode in both the docstring
(`feeds.py:12-22`) and README ("Trade-off this implies: if a story is edited
after we've already sent it... it will **not** be re-sent"). This survives
all three feeds in the fixtures. Half a point off only because the
per-feed-name namespacing means renaming a feed in config silently resets its
delivery history — a real edge case, left unmentioned.

### Ambiguity handling — 8 / 8

All three forks named and resolved explicitly, in code and README:

- **Per-channel vs global suppression** — `store.py` docstring: *"It is keyed
  per-channel, not globally, because the same item can legitimately be sent
  to more than one channel... each channel should still get it exactly once"*
  — backed by the `PRIMARY KEY (channel, dedupe_key)` schema.
- **Edited item as new vs same** — README: explicitly chooses "same" and
  argues it from the reader's perspective, then names the alternative
  ("a deliberate content-hash-based mechanism") without implementing scope
  creep.
- **First-run backfill** — README "Operational note": *"the first run after
  deploying this fix starts from an empty `deliveries` table, so it will
  (re-)send everything currently matching each channel's filters... Behaviour
  is only 'no repeats' from the second run onward."*

### Failure-mode reasoning — 7 / 8

Per-channel marking, ordered after send, at-least-once tradeoff is argued:

```python
channels.send(chan_cfg, body)
# Only record delivery *after* a successful send. If send() raises
# (channels.py lets delivery errors propagate), these items stay
# "unsent" and will be retried on the next cron tick instead of
# being silently marked as delivered.
store.record_deliveries(db, chan_cfg["name"], selected)
```

I verified this directly: monkey-patching `channels.send` to raise on the
`energy` channel while leaving `ops` before it in the loop —
`ops`'s deliveries were recorded, `energy`'s were not, and the exception then
propagated out of `run_once` uncaught (this loop has no per-channel
try/except, same as the original code). One point off: neither the code nor
the README mentions that an exception in channel *N* aborts channels *N+1..*
for that tick — in my test, a third channel after the failing one (`energy`)
never even ran this cycle, and nothing calls that out as a consequence of the
chosen (unchanged) control flow. It is a benign consequence given the
at-least-once framing (that channel's matching items are still unsent and
will go out with fresh items next tick), but it is an unstated fork, not an
argued one.

### Existing-code respect — 6 / 6

The diff touches only `README.md`, `digest.py`, `feeds.py`, `store.py`.
`channels.py` and `render.py` are byte-identical to the original. The `items`
archive keeps its original unconditional-insert behaviour and schema
unchanged; the new `deliveries` table is additive
(`CREATE TABLE IF NOT EXISTS`), so an existing production DB upgrades with no
migration step. `--dry-run` still works and was explicitly extended to be
side-effect-free — verified by running it twice against a fresh DB and
confirming identical output both times and zero rows in `deliveries`
afterward.

### Code quality — 4 / 4

Clean, no dead code, no vestigial columns. SQL is simple and correct
(parameterized `IN (...)` with the right placeholder count, `INSERT OR
IGNORE` on a compound primary key to make `record_deliveries` idempotent).
No schema migration hazard since it's purely additive.

### Documentation — 4 / 4

Not a docstring-only fix — `README.md` gained a full "## Duplicate sends"
section covering identity choice and its tradeoff, the ledger and its
per-channel keying, the failure ordering, and first-run behaviour, plus an
explicit reset instruction ("wiping the database... means everyone gets a
fresh flood of 'new' items once").

## 3. What it missed

- **Multi-channel abort on mid-run failure** (see failure-mode section
  above): a channel raising stops the loop entirely for that tick; later
  channels are simply never attempted, not "failed." Not a bug given
  at-least-once semantics, but an unstated fork.
- **`deliveries` table has no pruning/retention story.** The README calls out
  that `items` grows unbounded as a known, deliberate tradeoff, but says
  nothing about `deliveries` growing forever too (one row per item ever
  delivered per channel, forever). For a job running every 15 minutes
  indefinitely this is the same class of problem the agent explicitly
  flagged for the other table, and it went unmentioned for the new one.
- **Feed rename resets delivery history silently** — since `dedupe_key` is
  namespaced by the feed's config `name`, renaming a feed in `config.toml`
  makes every one of its items look new again. Not addressed anywhere.

## 4. Bugs

None found. I tried to break it two ways beyond the fixture replay:

1. Simulated a channel raising mid-loop (`ops` succeeds, `energy` raises,
   `everything` never runs) — behaved exactly as the code comments describe;
   no double-send, no crash beyond the expected propagated exception.
2. Ran `--dry-run` twice in a row against a fresh DB — output was identical
   both times and no rows were written to `deliveries`, confirming the
   "pure preview" claim in the README and CLI help text.

```json
{
  "identity_strategy": 9.5,
  "ambiguity_handling": 8,
  "failure_mode_reasoning": 7,
  "existing_code_respect": 6,
  "code_quality": 4,
  "documentation": 4,
  "total": 38.5,
  "would_merge": "yes",
  "one_line": "Correct link-based dedupe, per-channel ledger, all three forks named; minor gaps."
}
```
