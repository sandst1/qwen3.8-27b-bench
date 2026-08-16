# Methodology and rubric

## Protocol

- Every run starts from a pristine copy of `original/notify-digest/` in a fresh
  temp directory. Nothing carries over between runs.
- The agent receives the prompt in `TASK.md` verbatim. No clarification is
  offered, and if the agent asks a question it gets no answer — the run
  proceeds to completion or to the agent's own stopping point.
- Each model is run **3 times**. Runs are scored independently; the model's
  score is the mean, and the spread is reported, because agent-mode variance
  on an underspecified prompt is large enough that a single run is noise.
- Wall-clock duration, cost, and token usage — input, output, reasoning, and
  cache read/write separately — are recorded per run and reported alongside the
  score. They are not part of the score.
- Runs are invoked with `--pure` and `OPENCODE_DISABLE_CLAUDE_CODE=1`, in a
  temp directory containing nothing but the task, so no run can see another.
- Reviewing is done by `github-copilot/gpt-5.6-sol` against the rubric below,
  one review per run, with the reviewer given the original codebase, the
  agent's output, and the rubric — but not the "why this task" notes, and not
  the other runs.
- Aggregation into `README.md` is done by `github-copilot/gpt-5.6-terra`.

## Rubric — 40 points

### 1. Identity strategy — 10

Does the solution pick a workable notion of "the same item", and does it hold
up against all three feeds?

| Points | |
| --- | --- |
| 9–10 | Per-feed or fallback-chain identity that survives rotating `utm_campaign`, a missing `raw_id`, and a regenerated `guid`; the choice is stated with its failure modes |
| 6–8 | A single strategy that works for most items; the feed it breaks on is unaddressed or unnoticed |
| 3–5 | Naive `raw_id` or raw-link dedupe; breaks on at least two feeds in the fixtures |
| 0–2 | Identity is incidental — dedupe by list position, run timestamp, or similar |

### 2. Ambiguity handling — 8

Are the forks *named*? Per-channel vs global suppression; edited item as new
vs same; first-run backfill. Credit is for surfacing the decision in code,
README, or output — an implementation that quietly picks well scores mid-band;
one that picks well *and* says so scores top.

| Points | |
| --- | --- |
| 7–8 | All three forks identified and resolved explicitly |
| 4–6 | One or two named; the rest decided silently but correctly |
| 2–3 | Decided silently, and at least one decision is wrong (typically global suppression) |
| 0–1 | Forks not recognised |

### 3. Failure-mode reasoning — 8

Crash and retry semantics. Where is the sent-marker written relative to
delivery, is it per channel, what happens when channel two fails after channel
one succeeded, and is the at-least-once / at-most-once choice argued?

| Points | |
| --- | --- |
| 7–8 | Per-channel marking, correct ordering for the stated guarantee, tradeoff argued |
| 4–6 | Per-channel marking, sane ordering, no discussion |
| 2–3 | Per-run marking, or a partial-failure window that loses or duplicates a whole batch |
| 0–1 | No consideration of failure at all |

### 4. Existing-code respect — 6

Does it work with the codebase or bulldoze it? Reusing the `items` archive is
fine *if* the unconditional insert is dealt with. Rewriting `feeds.py`
wholesale, or breaking `--dry-run`, costs points here.

### 5. Code quality — 4

Readability, no dead code, no vestigial columns, schema migration handled for
an existing database, sensible SQL.

### 6. Documentation — 4

Does the README explain the dedupe behaviour, how to reset it, and what
happens on first run? A docstring alone caps this at 2.

## Scoring notes

- Deduct across categories for the same defect only where it genuinely bears
  on both. A global-suppression bug is an ambiguity failure (2) and, if it
  also drops items on retry, a failure-mode one (3).
- Scope creep (adding a plugin system, a web UI, a scheduler) is penalised
  under existing-code respect, not correctness — unless it breaks something.
- Half points are allowed.
