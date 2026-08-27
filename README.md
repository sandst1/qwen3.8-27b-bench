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
| qwen3.8-flash-next-q4_k_xl | 36.25 (35.5–37.5) | 4 | 13m 40s (12m 00s–15m 37s) | 563,876 | $0.00 |
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

`processed` is input + output + reasoning with cached reads added back: the only token figure comparable across providers and local quants. `total` is recorded per run but omitted because it excludes cached reads, and a re-read prefix may be reported as `input` or `cache_read` depending on the serving stack. `qwen3.8-27b-nvfp4-reasoning-medium` and `qwen3.8-27b-nvfp4-nothink` report `cache_read` near zero; the other labels do not. Cost is not a token ranking.

The locally served labels span 1.3–4.6x between fastest and slowest runs; hosted labels span 1.2–2.0x. That variation separates the setups rather than decorating the table.

No label reports non-zero `mean_tokens.reasoning`, so there is no reasoning column. Zero does not mean a model did not think: these providers fold thinking into `output`; the NVFP4 medium-reasoning variant averages 23,374 output tokens against 5,051 for its no-think variant.

| Model | Identity | Ambiguity | Failure modes | Existing code | Code quality | Docs |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| claude-opus-5-max | 9.63 | 8.00 | 8.00 | 5.88 | 4.00 | 4.00 |
| claude-opus-5 | 9.63 | 8.00 | 8.00 | 6.00 | 3.88 | 3.75 |
| qwen3.8-flash-next-q4_k_xl | 9.50 | 7.00 | 7.25 | 6.00 | 3.50 | 3.00 |
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

**claude-opus-5-max** — 40.0 / 39.5 / 39.5 / 39.0. All runs agree on the central delivery design; identity varies only in fallback detail.
- *Good.* All runs use per-channel ledgers, send then mark, isolate a failed channel, and migrate existing databases. Runs 1–2 use per-format stable fields, run 3 falls back from canonical link to source-scoped ID and content hash, and run 4 aliases multiple keys.
- *Bad.* Run 1 creates an empty SQLite schema during dry-run; run 2 writes `items` during dry-run. Runs 3–4 omit reset/inspection or duplicate-suppression visibility, and no run resolves concurrent-cron handling.

**claude-opus-5** — 39.5 / 39.5 / 39.0 / 39.0. All runs keep per-channel send-then-mark semantics.
- *Good.* Every run uses a per-format identity chain, a per-channel ledger, post-delivery state, and a migration. Runs 1, 3, and 4 isolate failures; run 2 verifies recovery, but its review does not establish continued processing after a failed channel.
- *Bad.* Run 1 says the dry-run records nothing while it archives rows. Runs 2–4 leave reset, retention, feed-rename, or edit-resend behavior incomplete; run 3 also includes a stray generated config.

**qwen3.8-flash-next-q4_k_xl** — 37.5 / 36.5 / 35.5 / 35.5. The ledger design is stable, but no run fully isolates delivery failures.
- *Good.* All runs use link-first identity, a per-channel ledger, post-send markers, and additive migration; fixture traps pass. Run 3 uses a per-feed `dedupe_by` rule, while the other runs use canonical-link variants.
- *Bad.* Runs 1, 3, and 4 let an exception skip later channels; run 4 also treats query-order changes as new items. Run 2 re-sends current feed contents once when its legacy ledger is deployed; run 3 backfills raw-ID archive rows with link IDs and creates duplicates.

**claude-sonnet-5-max** — 39.5 / 38.5 / 33.5 / 31.0. Run 1's global whole-run marker is the central instability; runs 2–4 instead use per-channel state.
- *Good.* All runs handle the fixture identities and migrate existing databases. Runs 2–4 send then mark per channel; run 2 also isolates failures, unlike runs 3–4.
- *Bad.* Run 1's global marker duplicates successful channels after a later failure. Runs 3–4 leave exceptions uncaught and starve later channels; runs 1 and 4 omit first-run/reset guidance, while run 2 lacks tests and retention policy.

**qwen3.8-flash-next-q3_k_xl** — 38.5 / 37.0 / 33.5 / 32.5. All runs agree on per-channel post-send state, but not identity: runs 1 and 3 use stable feed fields, runs 2 and 4 normalized URLs.
- *Good.* Every run has a persistent per-channel ledger, post-send marker, additive migration, and fixture-correct suppression. Run 4 scopes URL paths by source; runs 1 and 3 use per-format identities.
- *Bad.* Every run leaves a failed channel able to starve later channels. Runs 1–2 omit first-run/reset and permit unbounded archive growth; run 2 also permits cross-feed link collisions, and run 3 falsely says dry-run leaves the DB untouched while it writes `items`.

**qwen3.8-27b-nvfp4-reasoning-medium** — 36.0 / 35.5 / 34.0 / 34.0. The central design is consistent: per-channel post-send state, but no failure isolation.
- *Good.* All runs use additive migration and per-channel send-then-mark state; fixture traps pass. Runs 1–3 select per-format stable fields, while run 4 uses source plus queryless link.
- *Bad.* Every run leaves an uncaught failure to starve every later channel. Runs 1–3 omit first-run/reset guidance; run 1 also lets same-batch duplicates through, and run 4 leaves an application-level archive-dedupe race.

**qwen3.8-27b-4bit-reasoning-medium** — 37.5 / 37.0 / 31.5 / 27.5. Run 3 reverses the otherwise per-channel post-send policy and causes data loss; run 4 cannot use an existing database.
- *Good.* All runs handle fixture identity and address archive insertion. Runs 1–2 use real migrations and per-channel send-then-mark; run 4 also uses post-send state but has no usable migration.
- *Bad.* Run 1 backfills wire rows by GUID and creates duplicate archive rows. Run 2 says dry-run records nothing while it writes `items`; run 3 marks globally before delivery and permanently loses a failed/later channel's items; run 4 raises `sqlite3.OperationalError` on a pre-fix database.

**qwen3.8-27b-2bit-reasoning-medium** — 37.0 / 35.0 / 30.5 / 26.5. Run 4 changes the central design to global mark-before-delivery; the other runs are per-channel post-send.
- *Good.* Runs 1–3 use additive migration and per-channel send-then-mark state. Runs 1 and 4 use source plus queryless link, run 2 uses per-format stable fields, and all runs address archive insertion.
- *Bad.* Run 3's raw-link identity re-sends newsroom entries 84121 and 84118 when `utm_campaign` changes. Run 4's global pre-send mark permanently loses items on a failed channel; runs 1–2 also leave later-channel abort and first-run/reset treatment incomplete.

**claude-sonnet-5** — 35.5 / 31.0 / 29.5 / 29.0. The runs disagree on identity and state scope; run 4 uses global mark-before-delivery.
- *Good.* All runs derive identity and add persistent state. Runs 1–3 send then mark per channel, and all migrate the ledger; run 1 uses per-format fields, while run 4 returns to per-format identity despite its global notification state.
- *Bad.* Run 2's `raw_id or link` re-sends edited wire items, and run 3 re-sends newsroom items after UTM rotation. Run 4's global pre-send marker permanently loses failed/later deliveries; runs 1 and 3 leave archive growth, and no run documents first-run/reset behavior.

**claude-opus-4.6-max** — 33.0 / 30.0 / 27.5 / 25.5. Per-channel post-send delivery is stable, but identity and migration regress in later runs.
- *Good.* Every run uses a per-channel ledger and marks after delivery. Runs 1–3 use link-based identity that passes the fixture churn, and runs 1–2 use additive schema changes.
- *Bad.* Runs 3–4 create a unique archive index without deduplicating old rows and crash on legacy databases. Run 4 also uses raw links and re-sends UTM-rotated newsroom items; runs 1–3 leave the README untouched.

**ornith-1.5-35b-a3b-8bit** — 35.0 / 26.0 / 25.0 / 24.5. Run 2 is the only per-channel post-send result; the other runs use global or whole-run state.
- *Good.* All runs use source plus canonical/queryless link identity and handle fixture churn. Run 2 uses an additive per-channel ledger and post-send markers; run 1 also migrates an existing database.
- *Bad.* Run 1's global `sent_at` state loses a failed channel's items and prevents a new channel receiving history. Run 3 crashes on a legacy DB and duplicates earlier sends after a later failure; run 4 marks unmatched items sent and duplicates successful channels after a later failure.

**ornith-1.5-35b-a3b-4bit** — 30.0 / 29.5 / 24.0 / 20.0. Run 3's global archive state and stateful dry-run are the central break; runs 1, 2, and 4 are per-channel post-send.
- *Good.* Every run uses canonical-link identity and handles fixture churn. Runs 2 and 4 use additive ledger migration and post-send markers; run 1 has the same marker placement but no usable legacy migration.
- *Bad.* Run 1 crashes because the existing DB lacks `items.key`. Run 3 lets dry-run consume state, loses items on failure, blocks new channels from history, and grows the archive; every run leaves the README unchanged.

**claude-sonnet-4.6-max** — 28.5 / 24.0 / 21.5 / 18.0. Global item-level suppression, not per-channel delivery tracking, is universal.
- *Good.* Runs 2–3 use feed-aware or UTM-stripped identity; run 4 uses canonical link. Run 4 marks after the channel loop, unlike runs 1–3, which mark before sending.
- *Bad.* Runs 1–3 mark before send, permanently lose failed/later deliveries, and runs 1–2 let dry-run poison state. Run 4 globally consumes unmatched items, so a later energy channel cannot receive them.

**github-copilot-claude-sonnet-4.6** — 29.0 / 21.0 / 19.0 / 18.5. The runs disagree on identity, state scope, timing, and migration; only run 3 is coherent end to end.
- *Good.* Run 3 uses source-and-UTM-normalized fingerprints, per-channel post-send state, and inert dry-run. Run 1 also uses per-channel post-send state; run 2's fallback identity handles the fresh-database fixtures.
- *Bad.* Run 1's raw link re-sends newsroom items and dry-run consumes state. Run 2 has global pre-send loss and no legacy migration; run 3 leaves archive duplication despite documenting it as ignored; run 4 combines raw-link UTM resend with global pre-send loss.

**github-copilot-claude-opus-4.6** — 24.0 / 21.5 / 20.0 / 19.5. Per-channel post-send delivery is universal, but raw-ID-first identity is not sufficient.
- *Good.* All runs keep `sent_items` scoped to item and channel, mark after `send()`, and leave dry-run state-free. Runs 1 and 4 use additive ledger tables.
- *Bad.* Runs 1, 3, and 4 re-send edited wire items because GUIDs are preferred. Run 2's `raw_id NOT NULL` silently drops blogroll items; run 3 crashes creating a unique index on duplicate legacy rows; run 4 never dedupes blogroll `NULL` IDs.

**qwen3.8-27b-nvfp4-nothink** — 28.0 / 25.5 / 18.5 / 6.0. Run 1 made no attempt; runs 2–4 use global rather than per-channel state.
- *Good.* Runs 2–3 normalize `utm_*` and pass fixture identity checks. Run 4 marks after each successful channel, and run 2 backfills global state from the archive; there is no universal sound design.
- *Bad.* Run 1 leaves duplicate sending unchanged. Runs 2–3 mark globally before delivery and permanently lose failed batches; run 4 re-sends edited wire items, withholds items from later channels after failure, and crashes creating its legacy unique index.

### Patterns

- A `(channel, identity)` ledger written only after that channel sends is near-universal among the strongest results: both Opus 5 labels use it in every run. Global or pre-send state repeatedly loses failed/later deliveries, duplicates successful channels, or consumes history needed by new channels.
- Feed-aware identity separates results: raw links re-send newsroom items after UTM rotation, raw-ID-first re-sends edited wire items, and raw-ID-only fails blogroll's `NULL` IDs. Per-format stable fields or normalized source-qualified links survive the fixture changes.
- Correct marker timing is not sufficient when `DeliveryError` aborts the loop. Qwen flash, NVFP4-medium, and Sonnet 5 max runs commonly preserve retryability yet indefinitely starve channels after a persistently broken earlier channel.
- Additive ledger tables usually migrate existing databases. Adding item columns or unique indexes without deduplicating/backfilling old rows either leaves the change inert or crashes startup.
- More time or money did not reliably buy better judgment: `claude-opus-5-max` averaged 39.50 in about 11 minutes for $2.91, while several slower local reasoning labels scored 32.25–34.88. Within the same NVFP4 stack, medium reasoning averaged 34.88 versus 19.50 for no-think, though the no-think set includes a 15-minute no-attempt.

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
