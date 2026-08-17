# notify-digest bench

A single-task benchmark for **architectural judgment under ambiguity**, in the
spirit of [ambiguity-coding-bench](https://github.com/sandst1/ambiguity-coding-bench).
One underspecified prompt, one small existing Python codebase, four runs per
model, scored out of 40 by a reviewer model.

## The task

A cron-driven digest mailer sends the same items over and over. The prompt is:

> We run digest.py from cron every 15 minutes. People keep getting the same
> items over and over. Fix it.
>
> Leave the codebase in a state where the next person to touch it understands
> what you chose and why.

That is the whole prompt. "Remember what you've sent" is a one-line fix and it
is wrong: the three feeds disagree about what an item's identity even is (one
rotates a `utm_campaign` on every URL, one has no identifier at all, one
regenerates its `guid` whenever an item is edited), three channels with
overlapping filters mean "already sent" is ambiguous between global and
per-channel, and the send loop can fail halfway through. See
[TASK.md](TASK.md) for the full set of buried decisions and
[BENCHMARK.md](BENCHMARK.md) for the rubric.

## Running it

```sh
python3 bench.py run    --model anthropic/claude-opus-5 --runs 4
python3 bench.py run    --model anthropic/claude-opus-5 --variant high
python3 bench.py review --model anthropic-claude-opus-5
python3 bench.py gather
```

`--model` goes straight to `opencode --model`; `--variant` to `--variant`
(provider reasoning effort), and it becomes part of the results label so
effort levels of the same model are tracked separately.

Each run gets a fresh temp copy of `original/notify-digest/`, `git init`ed with
a single pristine commit so the reviewer can diff. The finished tree lands in
`results/<label>/run-N/` alongside `_metrics.json`, the raw `_events.jsonl`,
`_agent.log`, and after review `_review.md` and `_scores.json`.

### Isolation

Runs cannot see each other. Each one is a lone `notify-digest/` inside its own
`tempfile.TemporaryDirectory`, deleted as soon as the output is copied out —
no sibling `run-N` directories, no `results/`, nothing of the benchmark
reachable by walking up from the work tree. Runs are invoked with `--pure` (no
external plugins) and `OPENCODE_DISABLE_CLAUDE_CODE=1` so a stray
`~/.claude/CLAUDE.md` or global skill doesn't help one model and not another.
Sessions are never continued or forked, so each run starts with empty context.

What is *not* isolated, by design: your `opencode.json`, `AGENTS.md`, and auth
live in the usual places and apply to every run equally.

### Metrics

`opencode run --format json` emits the raw event stream, which carries
`tokens: {input, output, reasoning, cache: {read, write}}` and `cost` on every
`step-finish` part. `_metrics.json` records all five counts plus cost, since a
step-finish is the additive unit — one assistant turn contains many, so
message-level totals undercount and are kept only as
`message_total_crosscheck`. `total` is input + output + reasoning with cached
reads reported but not added in, and `processed` adds them back. Both are
recorded, but only `processed` is tabled below: whether a re-read prompt prefix
is billed as `input` or as `cache_read` is a property of the serving stack, so
`total` is comparable only between labels served the same way, while `processed`
is comparable everywhere. Providers here
report `reasoning` as 0 and fold thinking into `output`; `reasoning_chars`
carries the exported thinking text when there is any, and was 0 for every run in
the table below. Subagent work happens in child sessions, found
via `parentID` from `opencode session list` and pulled in with `opencode
export`. `source` in the metrics tells you which path produced the numbers.

Wall clock is measured around the subprocess, so it includes opencode startup
and MCP cold boot. If that bothers you, run `opencode serve` and it'll drop.

Reviews are written by `github-copilot/gpt-5.6-sol`, aggregation by
`github-copilot/gpt-5.6-terra`; override with `--reviewer` / `--gatherer`.
`bench.py gather --dry-run` regenerates `results/summary.json` without invoking
a model, which is the fastest way to sanity-check scoring.

## Rubric

| Category | Points |
| --- | --- |
| Identity strategy | 10 |
| Ambiguity handling | 8 |
| Failure-mode reasoning | 8 |
| Existing-code respect | 6 |
| Code quality | 4 |
| Documentation | 4 |
| **Total** | **40** |

## Results

<!-- BENCH:RESULTS:BEGIN -->

| Model | Score / 40 | Runs | Time | Tokens processed | Cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| claude-opus-5-max | 39.50 (39.0–40.0) | 4 | 10m 59s (9m 02s–13m 23s) | 2,345,748 | $2.91 |
| claude-opus-5 | 39.25 (39.0–39.5) | 4 | 5m 25s (4m 22s–5m 59s) | 890,341 | $1.31 |
| claude-sonnet-5-max | 35.62 (31.0–39.5) | 4 | 9m 08s (6m 49s–11m 59s) | 2,127,746 | $1.12 |
| qwen3.8-27b-nvfp4-reasoning-medium | 34.88 (34.0–36.0) | 4 | 18m 34s (13m 48s–28m 52s) | 640,063 | $0.00 |
| qwen3.8-27b-4bit-reasoning-medium | 33.38 (27.5–37.5) | 4 | 22m 26s (13m 08s–26m 00s) | 747,066 | $0.00 |
| qwen3.8-27b-2bit-reasoning-medium | 32.25 (26.5–37.0) | 4 | 13m 29s (5m 01s–22m 55s) | 620,248 | $0.00 |
| claude-sonnet-5 | 31.25 (29.0–35.5) | 4 | 2m 37s (1m 44s–3m 32s) | 574,062 | $0.33 |
| claude-opus-4.6-max | 29.00 (25.5–33.0) | 4 | 5m 29s (4m 23s–6m 43s) | 578,206 | $1.07 |
| claude-sonnet-4.6-max | 23.00 (18.0–28.5) | 4 | 6m 52s (4m 39s–8m 24s) | 514,191 | $0.59 |
| github-copilot-claude-sonnet-4.6 | 21.88 (18.5–29.0) | 4 | 3m 01s (2m 41s–3m 14s) | 206,790 | $0.22 |
| github-copilot-claude-opus-4.6 | 21.25 (19.5–24.0) | 4 | 52s (35s–1m 10s) | 69,349 | $0.18 |
| qwen3.8-27b-nvfp4-nothink | 19.50 (6.0–28.0) | 4 | 8m 13s (4m 37s–15m 29s) | 338,515 | $0.00 |

`processed` is input + output + reasoning with cached reads added back — the
whole context volume a run pushed through the model, which is the one token
figure comparable across serving stacks. The marginal count that excludes
cached reads is in each `_metrics.json` as `total`, but it is not tabled here:
whether a re-read prompt prefix is billed as `input` or as `cache_read` is a
property of the serving stack, and both NVFP4 labels report zero cached reads
while the others do not, so the column would be comparing bookkeeping rather
than work. Cost is the pricing result, not a token ranking.

Run-time variance is material: the local labels span 2.0–4.6x from fastest to
slowest run, while the hosted labels span 1.2–2.0x. The score spreads are not
mere noise either: several reflect a switch between safe per-channel,
post-delivery state and global or pre-delivery suppression.

No label reports non-zero `mean_tokens.reasoning`, so there is no reasoning
column. That does not show an absence of thinking: providers fold it into
`output`. The same NVFP4 model illustrates this directly: medium reasoning
averaged 23,374 output tokens versus 5,051 with no-think enabled.

| Model | Identity | Ambiguity | Failure modes | Existing code | Code quality | Docs |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| claude-opus-5-max | 9.63 | 8.00 | 8.00 | 5.88 | 4.00 | 4.00 |
| claude-opus-5 | 9.63 | 8.00 | 8.00 | 6.00 | 3.88 | 3.75 |
| claude-sonnet-5-max | 9.38 | 6.75 | 6.25 | 5.88 | 4.00 | 3.38 |
| qwen3.8-27b-nvfp4-reasoning-medium | 9.13 | 6.25 | 6.63 | 5.88 | 3.75 | 3.25 |
| qwen3.8-27b-4bit-reasoning-medium | 9.13 | 6.50 | 6.00 | 5.13 | 3.13 | 3.25 |
| qwen3.8-27b-2bit-reasoning-medium | 8.38 | 5.00 | 5.88 | 6.00 | 3.75 | 3.25 |
| claude-sonnet-5 | 8.13 | 5.88 | 5.63 | 5.13 | 3.63 | 2.88 |
| claude-opus-4.6-max | 8.00 | 5.50 | 5.25 | 5.38 | 2.75 | 2.13 |
| claude-sonnet-4.6-max | 7.25 | 3.25 | 2.25 | 3.88 | 3.38 | 3.00 |
| github-copilot-claude-sonnet-4.6 | 6.25 | 3.75 | 4.25 | 3.25 | 2.75 | 1.63 |
| github-copilot-claude-opus-4.6 | 5.00 | 3.38 | 5.00 | 3.88 | 2.38 | 1.63 |
| qwen3.8-27b-nvfp4-nothink | 5.75 | 3.00 | 1.88 | 3.75 | 1.75 | 2.38 |

### Notes

Each label ran four times; the per-run scores lead each entry, high to low.
Where the runs disagreed about the central design — per-channel or global
suppression, marking before or after delivery — that disagreement is the first
thing said about the label.

**claude-opus-5-max** — 40.0 / 39.5 / 39.5 / 39.0. No run departs from the
central design.

- *Good.* Every run keyed a per-channel ledger on `(channel, source, identity)`
  and marked only after that channel's send returned, isolating a failed
  channel so the rest of the tick still ran. Identity followed the feed:
  newsroom `entry_id`, blogroll permalink, wire link in preference to the
  churning `guid`. Every run shipped both a migration and a documented
  first-run escape hatch (`--mark-seen` or `--seed`).
- *Bad.* Dry-run leaks: run-1 creates an empty database and run-2 still writes
  archive rows, in both cases against wording that promises otherwise. The
  tracking-parameter allowlist is fixed, so a new provider parameter reopens
  the duplicate. Run-4 namespaces its ID key by source but not its URL key, and
  no run discusses overlapping cron invocations.

**claude-opus-5** — 39.5 / 39.5 / 39.0 / 39.0. The tightest label in the table.

- *Good.* Per-channel `deliveries` ledger, post-delivery marking, and an
  explicit at-least-once argument in all four runs; two of them verified
  failure isolation against a deliberately broken channel. Every README
  documents the identity table and the per-channel rationale.
- *Bad.* Run-1 claims dry-run records nothing while it writes the archive.
  Run-2 ships no tests and leaves `store.count_sent()` dead. Run-3 documents no
  reset procedure. Run-4's content-hash fallback is weak for link-less edited
  items, and its 90-day retention is a constant with no knob.

**claude-sonnet-5-max** — 39.5 / 38.5 / 33.5 / 31.0. Run-1 is a different
design from the other three, and that is the whole spread.

- *Good.* Runs 2–4 key on a tracking-stripped, source-qualified link that
  survives all three fixture traps, with per-channel state marked immediately
  after each successful send. Run-2's README names all three forks —
  per-channel scope, edit-as-same, first-run catch-up — and gives a deployment
  sequence for an existing box.
- *Bad.* Run-1 suppresses globally through the `items` archive and marks the
  whole batch after the channel loop, so a partial failure duplicates the
  channels that already succeeded. Runs 3 and 4 leave the channel loop uncaught,
  so one channel's exception starves the rest of that tick. First-run flood and
  reset are undocumented in runs 1 and 4, and `deliveries` growth is unbounded.

**qwen3.8-27b-nvfp4-reasoning-medium** — 36.0 / 35.5 / 34.0 / 34.0. The only
local label whose four runs agreed on both suppression scope and marker timing,
and the only one with a spread under 10 points.

- *Good.* All four runs used a per-channel `sent` ledger marked after a
  successful send, and all four were verified against the snapshot-a→b
  transition: UTM rotation, `guid` regeneration, and in-place edits all
  suppressed. `feeds.py`, `channels.py`, and `render.py` were left alone, and
  dry-run never writes to `sent`. Run-2 added a real legacy-database migration.
- *Bad.* Every run leaves `DeliveryError` uncaught, so channels after a failing
  one are skipped; run-2's own notes concede that a permanently broken middle
  channel starves the rest indefinitely. Runs 1, 2, and 4 never mention
  first-run backfill or offer a reset lever, and run-1 lets two same-identity
  entries in a single fetch both through.

**qwen3.8-27b-4bit-reasoning-medium** — 37.5 / 37.0 / 31.5 / 27.5. The spread is
operational rather than stylistic: run-3 alone marks globally before delivery.

- *Good.* Runs 1, 2, and 4 mark per-channel after delivery, verified by
  simulating an outage — the failed channel retries without re-sending to the
  channels that succeeded. Runs 1–3 ship a schema migration for existing
  databases, and run-1's README names all three forks including first-run
  backfill.
- *Bad.* Run-3 marks items in the fetch loop before any delivery, so a simulated
  `energy` failure loses them permanently. Run-4 crashes on deploy against a
  pre-existing database (`table items has no column named norm_link`). Run-1's
  migration backfills `COALESCE(raw_id, link)` and so picks exactly the wire
  `guid` its own comment says to avoid, and run-2's README claims dry-run
  records nothing while it writes six archive rows.

**qwen3.8-27b-2bit-reasoning-medium** — 37.0 / 35.0 / 30.5 / 26.5. Its best run
is competitive with 4-bit's; its worst two are where the label loses the points.

- *Good.* Runs 1–3 keep per-channel state marked after send, verified against a
  partial failure. Run-2 picks per-format keys in `feeds.py` with inline
  comments naming the failure each one prevents, and documents 30-day retention
  alongside the per-channel scope.
- *Bad.* Run-3 keys on the raw `(source, link)` and re-sends newsroom items when
  `utm_campaign` rotates from w33 to w34. Run-4 marks globally in the feed loop
  before delivery and drops items for good when a channel is down. No run names
  the first-run backfill, and runs 1, 3, and 4 give no reset procedure.

**claude-sonnet-5** — 35.5 / 31.0 / 29.5 / 29.0. Run-4 breaks from the other
three on both scope and timing.

- *Good.* Runs 1–3 use a per-channel ledger marked after `channels.send()`
  succeeds. Runs 1 and 4 choose identity per feed rather than forcing one global
  field. All four migrate additively and keep dry-run out of the ledger.
- *Bad.* Run-4 marks a global `notified` table in the feed loop before delivery,
  so an uncaught `DeliveryError` drops items for the unreached channels and the
  next run sends nothing. Run-3's link-only identity re-sends UTM-rotated
  newsroom items. No run documents first-run backfill or a reset, and runs 1 and
  3 leave the unconditional archive insert growing every tick.

**claude-opus-4.6-max** — 33.0 / 30.0 / 27.5 / 25.5. Delivery semantics are
right in every run; identity and migration are where it loses the points.

- *Good.* All four runs scope suppression per channel, mark only after a
  successful send, upgrade an existing database additively, and preserve
  dry-run. Run-1's link-then-`raw_id` lookup survives all three fixture traps,
  and run-2 batches the unsent check into a single query.
- *Bad.* Runs 3 and 4 add a unique index over `items(source, link)` that raises
  on any legacy database carrying duplicates from the old unconditional insert.
  Run-4 keys on the raw link and re-sends UTM-rotated newsroom items while its
  new README asserts the opposite. Runs 1–3 never touch the README at all,
  leaving the Layout table stale, and no run mentions the deploy-time backfill.

**claude-sonnet-4.6-max** — 28.5 / 24.0 / 21.5 / 18.0. Identity improves run
over run; state handling never does.

- *Good.* Run-2's `link OR raw_id` key, scoped per source, clears all three
  traps and is documented as a per-format table. Runs 3 and 4 strip tracking
  parameters down to a canonical link. Three runs added test suites, and run-4's
  README covers the dedup key, dry-run behavior, and crash/retry.
- *Bad.* Every run suppresses globally — there is no channel dimension anywhere
  in the label. Runs 1–3 record items in the fetch loop before delivery, so a
  failed batch is consumed; run-4 marks after the loop but still globally and
  per run, duplicating `ops` on retry when `energy` fails. Runs 1 and 2 let a
  dry-run poison the state so the real run sends nothing, and run-4 marks items
  that matched no channel, putting them out of reach of any channel added later.

**github-copilot-claude-sonnet-4.6** — 29.0 / 21.0 / 19.0 / 18.5. The four runs
disagree on all three central axes, which is the most important thing about it.

- *Good.* Run-3 is coherent: a SHA-256 over the source and a UTM-stripped link,
  per-channel state, marking after send, and a dry-run that stays inert. Run-1
  also keeps per-channel post-delivery state and names the at-least-once
  tradeoff in comments, and run-2's `raw_id`-or-link fallback clears all three
  traps.
- *Bad.* Runs 2 and 4 mark globally at fetch time, so a `DeliveryError` erases
  items permanently. Runs 1 and 4 key on the raw link and re-send newsroom item
  84121 when its campaign rotates. Runs 1 and 2 consume dedup state during
  `--dry-run`, leaving the real run with nothing to send. Run-2 crashes on a
  pre-fix schema (`no column named dedup_key`), and even run-3's docstring
  claims duplicate archive rows are ignored when the insert is still
  unconditional.

**github-copilot-claude-opus-4.6** — 24.0 / 21.5 / 20.0 / 19.5. Delivery
semantics are unanimous and correct; identity ignores what the feeds actually
do.

- *Good.* All four runs scope `sent_items` to `(item, channel)`, mark only after
  `channels.send()` returns, leave dry-run state-free, and keep the diff
  surgical with `feeds.py`, `channels.py`, and `render.py` untouched. Run-4's
  additive `CREATE TABLE IF NOT EXISTS` upgrades a production database with no
  manual step.
- *Bad.* Three runs prefer the wire `guid`, so "Port fee inquiry opened"
  re-sends the moment its guid gains `-r2`. Run-2's `raw_id TEXT NOT NULL`
  silently swallows every blogroll item, none of which has an ID, and its
  `CREATE TABLE IF NOT EXISTS` no-ops on a legacy database so the spam simply
  continues. Run-3's unique index raises `IntegrityError` on existing
  duplicates. Not one run modified the README.

**qwen3.8-27b-nvfp4-nothink** — 28.0 / 25.5 / 18.5 / 6.0. The 6.0 is run-1,
which produced nothing at all: exit code -2, zero tokens, no tool calls, and a
codebase byte-for-byte identical to the original after 15m 29s.

- *Good.* Runs 2 and 3 strip `utm_*` and survive all three fixture traps. Runs
  2–4 add a README "Deduplication" section covering identity rationale, reset,
  and first-run behavior. Run-2 backfills seen-state from the existing archive
  on connect, and run-4 is the only run in the label to mark after delivery.
- *Bad.* No run scopes suppression per channel, so run-4's `ops` success
  followed by an `energy` crash robs the `everything` channel permanently. Runs
  2 and 3 commit seen-state before the channel loop, and a retry after
  `DeliveryError` sends zero. Run-4's `raw_id or link` prefers the guid and
  re-sends edited wire items while its README says link wins. Runs 3 and 4 have
  no working migration for a legacy database.

### Patterns

- Per-channel state written after each successful delivery is the clearest
  separator. It is universal in Opus 5 and present in the strong Sonnet 5 and
  Qwen runs; global or pre-send state causes the recurring permanent-loss and
  retry-duplication defects.
- Identity has to follow the feed. Raw links fail on newsroom UTM rotation; raw
  IDs fail on wire GUID churn or blogroll's missing ID. Feed-specific keys or
  tracking-normalized links handle all three fixture traps.
- Uncaught delivery errors are the common residual failure. Every NVFP4
  reasoning run can starve later channels despite correct marker timing; some
  Sonnet 5 and 2-bit runs do too. Opus 5 is the group that isolates those
  failures and continues.
- Existing-database migration separates prototypes from deployable changes.
  Adding unique indexes over legacy duplicates crashes multiple Opus 4.6,
  4-bit, and Copilot runs; additive delivery-ledger tables avoid that failure.
- More time and money did not reliably buy better judgment. Opus 5 averaged
  39.25 in 5m 25s for $1.31, while local reasoning labels took 13m 29s–22m 26s
  for 32.25–34.88; the slowest, 4-bit, averaged 33.38. Within the same NVFP4
  stack, medium reasoning beat no-think 34.88 to 19.50, though no-think includes
  a 15m 29s non-attempt.

<!-- BENCH:RESULTS:END -->

## Layout

```
bench.py                     runner: run / review / gather
TASK.md                      the prompt, plus the buried-decisions notes
BENCHMARK.md                 protocol and rubric
original/notify-digest/      pristine starting codebase
results/<label>/run-N/       agent output + _metrics.json + _review.md + _scores.json
results/summary.json         aggregated, regenerable with `gather --dry-run`
```
