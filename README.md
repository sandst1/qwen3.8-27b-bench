# notify-digest bench

A single-task benchmark for **architectural judgment under ambiguity**, in the
spirit of [ambiguity-coding-bench](https://github.com/sandst1/ambiguity-coding-bench).
One underspecified prompt, one small existing Python codebase, three runs per
model, scored out of 40 by a reviewer model.

## The task

A cron-driven digest mailer sends the same items over and over. The prompt is:

> We run digest.py from cron every 15 minutes. People keep getting the same
> items over and over. Fix it.

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
python3 bench.py run    --model anthropic/claude-opus-5 --runs 3
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
`message_total_crosscheck`. `total` is input + output + reasoning; cached reads
are reported but not added in. Subagent work happens in child sessions, found
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

_No runs yet. Run `bench.py run`, `bench.py review`, then `bench.py gather`._

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
