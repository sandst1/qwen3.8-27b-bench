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

The Qwen 3.8 variants & Ornith 1.5 were run locally with Asus Ascent GX10 (=DGX Spark) and the Claudes are from the cloud via Github Copilot.

<!-- BENCH:RESULTS:BEGIN -->

| Model | Score / 40 | Runs | Time | Tokens processed | Cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| claude-opus-5-max | 39.50 (39.0–40.0) | 4 | 10m 59s (9m 02s–13m 23s) | 2,345,748 | $2.91 |
| claude-opus-5 | 39.25 (39.0–39.5) | 4 | 5m 25s (4m 22s–5m 59s) | 890,341 | $1.31 |
| claude-sonnet-5-max | 35.62 (31.0–39.5) | 4 | 9m 08s (6m 49s–11m 59s) | 2,127,746 | $1.12 |
| qwen3.8-flash-next-q3_k_xl | 35.38 (32.5–38.5) | 4 | 7m 40s (5m 32s–12m 44s) | 286,925 | $0.00 |
| qwen3.8-27b-nvfp4-reasoning-medium | 34.88 (34.0–36.0) | 4 | 18m 34s (13m 48s–28m 52s) | 640,063 | $0.00 |
| qwen3.8-27b-4bit-reasoning-medium | 33.38 (27.5–37.5) | 4 | 22m 26s (13m 08s–26m 00s) | 747,066 | $0.00 |
| qwen3.8-27b-2bit-reasoning-medium | 32.25 (26.5–37.0) | 4 | 13m 29s (5m 01s–22m 55s) | 620,248 | $0.00 |
| claude-sonnet-5 | 31.25 (29.0–35.5) | 4 | 2m 37s (1m 44s–3m 32s) | 574,062 | $0.33 |
| claude-opus-4.6-max | 29.00 (25.5–33.0) | 4 | 5m 29s (4m 23s–6m 43s) | 578,206 | $1.07 |
| ornith-1.5-35b-a3b-8bit | 27.62 (24.5–35.0) | 4 | 12m 00s (10m 17s–13m 01s) | 689,393 | $0.00 |
| ornith-1.5-35b-a3b-4bit | 25.88 (20.0–30.0) | 4 | 8m 52s (6m 52s–11m 59s) | 777,324 | $0.00 |
| claude-sonnet-4.6-max | 23.00 (18.0–28.5) | 4 | 6m 52s (4m 39s–8m 24s) | 514,191 | $0.59 |
| github-copilot-claude-sonnet-4.6 | 21.88 (18.5–29.0) | 4 | 3m 01s (2m 41s–3m 14s) | 206,790 | $0.22 |
| github-copilot-claude-opus-4.6 | 21.25 (19.5–24.0) | 4 | 52s (35s–1m 10s) | 69,349 | $0.18 |
| qwen3.8-27b-nvfp4-nothink | 19.50 (6.0–28.0) | 4 | 8m 13s (4m 37s–15m 29s) | 338,515 | $0.00 |

`processed` is the whole context volume: input + output + reasoning with cached reads added back. It is the only token figure comparable across providers and local quants. `total` is recorded per run but left out because it excludes cached reads, and whether a re-read prefix is billed as `input` or `cache_read` depends on the serving stack. `qwen3.8-27b-nvfp4-reasoning-medium` and `qwen3.8-27b-nvfp4-nothink` report mean `cache_read` near zero; the other labels do not. Cost is not a token ranking.

Run-time spread separates the setups: local labels vary 1.3–4.6x from fastest to slowest run, while hosted labels vary 1.2–2.0x. The score spreads also reflect central design changes, especially switches between per-channel post-send state and global or pre-send suppression.

No label reports non-zero `mean_tokens.reasoning`, so there is no reasoning column. That does not mean the models did not think: these providers fold thinking into `output`. The same NVFP4 model averaged 23,374 output tokens with medium reasoning versus 5,051 with no-think.

| Model | Identity | Ambiguity | Failure modes | Existing code | Code quality | Docs |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| claude-opus-5-max | 9.63 | 8.00 | 8.00 | 5.88 | 4.00 | 4.00 |
| claude-opus-5 | 9.63 | 8.00 | 8.00 | 6.00 | 3.88 | 3.75 |
| claude-sonnet-5-max | 9.38 | 6.75 | 6.25 | 5.88 | 4.00 | 3.38 |
| qwen3.8-flash-next-q3_k_xl | 9.50 | 7.00 | 7.38 | 4.63 | 4.00 | 2.88 |
| qwen3.8-27b-nvfp4-reasoning-medium | 9.13 | 6.25 | 6.63 | 5.88 | 3.75 | 3.25 |
| qwen3.8-27b-4bit-reasoning-medium | 9.13 | 6.50 | 6.13 | 5.25 | 3.13 | 3.25 |
| qwen3.8-27b-2bit-reasoning-medium | 8.38 | 5.00 | 5.88 | 6.00 | 3.75 | 3.25 |
| claude-sonnet-5 | 8.13 | 5.88 | 5.63 | 5.13 | 3.63 | 2.88 |
| claude-opus-4.6-max | 8.00 | 5.50 | 5.25 | 5.38 | 2.75 | 2.13 |
| ornith-1.5-35b-a3b-8bit | 8.63 | 4.38 | 3.50 | 4.75 | 3.25 | 3.13 |
| ornith-1.5-35b-a3b-4bit | 8.00 | 4.25 | 4.88 | 4.13 | 3.00 | 1.63 |
| claude-sonnet-4.6-max | 7.25 | 3.25 | 2.25 | 3.88 | 3.38 | 3.00 |
| github-copilot-claude-sonnet-4.6 | 6.25 | 3.75 | 4.25 | 3.25 | 2.75 | 1.63 |
| github-copilot-claude-opus-4.6 | 5.00 | 3.38 | 5.00 | 3.88 | 2.38 | 1.63 |
| qwen3.8-27b-nvfp4-nothink | 5.75 | 3.00 | 1.88 | 4.75 | 1.75 | 2.38 |

### Notes

Each label ran four times; scores lead each entry in high-to-low order.

**claude-opus-5-max** — 40.0 / 39.5 / 39.5 / 39.0. All runs agree on per-channel, post-send state.
- *Good.* Every run uses feed-specific identity: newsroom `entry_id`, blogroll permalink, and wire link over the churning `guid`. Every run marks after successful delivery in a per-channel ledger; migration and tests are present, and successful channels remain marked when another fails.
- *Bad.* Run-1 misses first-run/backfill, reset, and legacy-duplicate handling; its review reports edited generic/wire items re-sending and index creation failing on old duplicates. Run-2 misses first-run/reset and channel-isolation documentation. Run-3 leaves later-channel starvation, fallback coverage, reset guidance, and regression tests unaddressed. Run-4 misses first-run/backfill and explicit edit policy.

**claude-opus-5** — 39.5 / 39.5 / 39.0 / 39.0. All runs keep per-channel send-then-mark semantics.
- *Good.* Every run uses a per-format identity chain that survives tracking changes, missing blogroll IDs, and regenerated wire GUIDs. Suppression is per channel, marking follows delivery, migration is reported in all runs, and dry-run does not consume delivery state.
- *Bad.* Run-1 has a README overstatement. Run-2 omits first-run/reset documentation and a broken channel can starve later channels. Run-3 omits tracking allowlisting, retention, feed-renames, and tests. Run-4 omits first-run/reset, edit semantics, channel-stall discussion, and tests.

**claude-sonnet-5-max** — 39.5 / 38.5 / 33.5 / 31.0. Run-1 records globally after the whole run; runs 2–4 use per-channel post-send state.
- *Good.* All runs handle the fixture identities. Runs 2–4 use per-channel ledgers marked after successful delivery; run-2 verifies legacy-duplicate migration, and run-1 also supplies migration work.
- *Bad.* Run-1's global whole-run marker duplicates already-successful channels after a later failure. Runs 3 and 4 leave delivery exceptions uncaught, starving later channels; run-4 also omits first-run and reset guidance, while run-2 lacks retention and an automated suite.

**qwen3.8-flash-next-q3_k_xl** — 38.5 / 37.0 / 33.5 / 32.5. All runs use per-channel post-send state, but identity differs between provider-specific keys (runs 1 and 3) and normalized URLs (runs 2 and 4).
- *Good.* Every run uses a persistent per-channel ledger marked after successful delivery and fixture-correct suppression. Runs 1 and 3 use feed-specific identity; run-3 documents first-run and retry behavior, while run-4 documents all three identity forks and has a tested backfill script.
- *Bad.* The `items` archive grows through unconditional inserts in runs 1, 2, and 4; run-3 leaves it append-only. Runs 1–3 abort later channels after an uncaught delivery error; run-3 also claims dry-run leaves the database untouched while it writes archive rows, and run-2 permits cross-source link collisions.

**qwen3.8-27b-nvfp4-reasoning-medium** — 36.0 / 35.5 / 34.0 / 34.0. All runs agree on per-channel send-then-mark state.
- *Good.* Runs 1–3 use feed-specific identities and run-4 normalized links; all pass the fixture transitions. All mark after successful delivery, preserving retries and successful-channel markers; migration work is reported and dry-run does not write sent state.
- *Bad.* Every run leaves delivery exceptions uncaught, so a broken middle channel starves every later channel. Run-3 also lacks fallback, reset tooling, tests, and smaller retry control. Run-1 omits first-run/reset, same-tick ordering, and intra-feed duplicates. Run-4 omits first-run, edit semantics, abort discussion, and an `items` uniqueness constraint.

**qwen3.8-27b-4bit-reasoning-medium** — 37.5 / 37.0 / 31.5 / 27.5. Runs 1, 2, and 4 use per-channel post-send state; run-3 marks before delivery.
- *Good.* Runs 1, 2, and 4 use fixture-verified identity, per-channel suppression, and post-send marking; run-2 supplies tests, migration, and detailed documentation. Run-3 has strong identity and migration work, and run-4 preserves dry-run normally.
- *Bad.* Run-1's migration backfills `COALESCE(raw_id, link)`, reviving wire GUID churn. Run-2 claims dry-run records nothing while writing archive rows and omits fresh-deployment/channel-starvation guidance. Run-3's pre-send mark loses the batch when `energy` fails. Run-4's unique index crashes on old duplicate archives.

**qwen3.8-27b-2bit-reasoning-medium** — 37.0 / 35.0 / 30.5 / 26.5. Runs 1–3 retain per-channel post-send state; run-4 switches to global pre-delivery marking.
- *Good.* Runs 1 and 2 handle the fixtures, with run-2 selecting newsroom ID, blogroll permalink, and wire link. Runs 1–3 mark after successful delivery; run-2 verifies partial failure and adds 30-day expiry, while run-1 reports migration support.
- *Bad.* Run-3's raw `(source, link)` key re-sends newsroom items when `utm_campaign` rotates. Run-4's global pre-send mark loses items when a channel fails. Run-1 misses first-run/reset and retention; run-2 misses first-run and starvation analysis; run-3 misses reset and retention configuration.

**claude-sonnet-5** — 35.5 / 31.0 / 29.5 / 29.0. Runs 1–2 use per-channel post-send state, run-3 has the wrong newsroom identity, and run-4 marks globally before delivery.
- *Good.* Runs 1 and 2 use per-feed identity, per-channel ledgers, post-send marking, and migration; run-3 keeps correct ledger timing and dry-run behavior. Run-4 documents a strong per-format identity, and all four keep the normal dedup state additive.
- *Bad.* Run-4's global pre-send mark loses every unreached delivery after `DeliveryError`. Run-3's link-only identity re-sends newsroom items after UTM rotation and grows archive rows. Run-1 also grows archive rows and omits crash/retry guidance; no run documents first-run backfill or reset, and run-2 intentionally lets wire edits resend.

**claude-opus-4.6-max** — 33.0 / 30.0 / 27.5 / 25.5. Scope and timing are consistent; identity and migration fail in runs 3–4.
- *Good.* All runs use per-channel ledgers, mark after delivery, and preserve dry-run. Runs 1–3 pass the fixtures; runs 1 and 2 use additive schema changes, and run-2 batches the unsent check.
- *Bad.* Run-3's unique index crashes on old duplicate archives. Run-4 has the same migration crash and raw-link identity re-sends newsroom items after UTM rotation. Run-1 misses README, first-run/backfill, reset, and failure-tradeoff documentation; later runs also omit reset, migration, test, isolation, or edit details.

**ornith-1.5-35b-a3b-8bit** — 35.0 / 26.0 / 25.0 / 24.5. Run-2 is the only coherent per-channel post-send design; runs 1, 3, and 4 use global or whole-run state.
- *Good.* All runs use link identity that handles the fixture changes. Run-2 verifies identity, first-run, dry-run, failure isolation, and additive migration; run-3 also uses per-channel post-send state. Run-1's identity and migration are otherwise sound.
- *Bad.* Run-1's global mark starves failed or newly added channels. Run-3 fails on an existing database and duplicates earlier channels after a later failure. Run-4 marks the whole pending set after the channel loop, drops unmatched items, and duplicates earlier deliveries after a later failure; it also omits README/reset coverage.

**ornith-1.5-35b-a3b-4bit** — 30.0 / 29.5 / 24.0 / 20.0. Runs 1, 2, and 4 use per-channel post-send ledgers; run-3 uses global archive suppression and consumes dry-run state.
- *Good.* Runs 1, 2, and 4 pass snapshot transitions with canonical-link identity and post-send marking. Run-2 adds tests and additive migration, and run-4's ledger works with an old database on the normal path.
- *Bad.* Run-1 crashes during the legacy `items.key` migration. Run-3's global state consumes dry-run, loses items on pre-send failure, and grows duplicate archive rows; it is the only run marked not mergeable. Runs 1, 2, and 4 omit some README, first-run, reset, retention, tests, or edit guidance.

**claude-sonnet-4.6-max** — 28.5 / 24.0 / 21.5 / 18.0. Identity improves across runs, but global suppression or bad timing remains in every run.
- *Good.* Run-2's per-source `link OR raw_id` and run-3/4 canonical links address the fixture identities. All runs add tests or migration work, and run-4 documents dedup, dry-run, and crash/retry behavior.
- *Bad.* Runs 1 and 2 let dry-run poison state; runs 1–3 mark before delivery; run-4 marks globally after the loop and duplicates `ops` when `energy` fails. Run-4 also marks unmatched items out of reach of later channels; the label generally omits first-run/reset and migration detail.

**github-copilot-claude-sonnet-4.6** — 29.0 / 21.0 / 19.0 / 18.5. The runs disagree on identity, scope, timing, and migration.
- *Good.* Run-3 has source-and-UTM-normalized fingerprints, per-channel state, post-send marking, and inert dry-run. Run-1 also has per-channel post-delivery state, and run-2's fallback identity clears the fixtures in a fresh database.
- *Bad.* Run-1's raw-link identity re-sends newsroom items and dry-run mutates state. Run-2 crashes on a pre-fix schema and breaks dry-run. Run-3 leaves archive duplication despite its docstring. Run-4 re-sends on UTM rotation and loses items on channel failure; the reviews repeatedly find missing README, reset, and first-run documentation.

**github-copilot-claude-opus-4.6** — 24.0 / 21.5 / 20.0 / 19.5. All runs agree on per-channel post-send delivery, but identity coverage is incomplete.
- *Good.* Every run scopes `sent_items` to item and channel, marks after `send()`, leaves dry-run state-free, and keeps unrelated feed/channel/render code untouched. Run-4's additive table creation upgrades a production database without a manual step.
- *Bad.* Runs 1, 3, and 4 fail fixture identities involving wire GUID churn, old duplicate archives, or blogroll rows with null `raw_id`. Run-2's `raw_id NOT NULL` silently drops blogroll items and its table creation does not migrate the legacy database. Run-3's unique index raises on existing duplicates; all runs have weak documentation.

**qwen3.8-27b-nvfp4-nothink** — 28.0 / 25.5 / 18.5 / 6.0. Run-1 made no attempt; runs 2–4 recognize identity but use global or per-item state.
- *Good.* Runs 2 and 3 normalize `utm_*` and pass fixture identity checks; runs 2–4 document more identity, reset, and first-run behavior. Run-4 is the only run marking after delivery, and run-2 backfills seen state from the archive.
- *Bad.* Run-1 leaves the duplicate-sending bug unfixed. Runs 2 and 3 mark globally before delivery, so `DeliveryError` permanently loses the batch and archive rows grow. Run-4 is not channel-scoped, prefers `raw_id`, re-sends edited wire items, and crashes migrating old duplicate databases.

### Patterns

- A persistent `(channel, item identity)` ledger checked before delivery and marked after success is the clearest separator. Global or pre-send state causes permanent loss, retry duplication, or starvation of newly added channels.
- Identity must follow the feed: newsroom `entry_id`, blogroll permalink, and wire link survive the fixtures. Raw links fail on newsroom UTM rotation; raw IDs fail on wire GUID churn and blogroll's missing ID.
- Uncaught channel errors are the common residual defect. Correct marker timing preserves retryability, but an exception still starves later channels; the strongest runs isolate those failures.
- Migration separates deployable fixes from prototypes. Additive delivery-ledger tables survive old archives; unique indexes over duplicate legacy rows crash several Opus 4.6, 4-bit, and Copilot runs.
- More time or money did not reliably buy better judgment: claude-opus-5-max averaged 39.50 in 10m 59s for $2.91, while local reasoning labels averaged 32.25–34.88 in 13m 29s–22m 26s at no listed cost. Within the NVFP4 stack, medium reasoning averaged 34.88 versus 19.50 for no-think; no-think includes a 15m 29s no-attempt.

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
