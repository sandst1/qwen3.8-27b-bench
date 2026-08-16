#!/usr/bin/env python3
"""notify-digest ambiguity benchmark runner.

    python3 bench.py run    --model <opencode-model> [--runs 3] [--label name]
    python3 bench.py review --model <label> [--reviewer github-copilot/claude-sonnet-5]
    python3 bench.py gather [--gatherer github-copilot/gpt-5.6-terra]

`run` copies original/notify-digest into a throwaway directory, points opencode
at it with the task prompt, times the run, and copies the result into
results/<label>/run-N/. `review` scores each run with the reviewer model.
`gather` hands every score and metric to the gatherer model, which rewrites the
results section of README.md.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ORIGINAL = ROOT / "original" / "notify-digest"
RESULTS = ROOT / "results"
TASK_MD = ROOT / "TASK.md"
RUBRIC_MD = ROOT / "BENCHMARK.md"

DEFAULT_REVIEWER = "github-copilot/claude-sonnet-5"
DEFAULT_GATHERER = "github-copilot/gpt-5.6-terra"
DEFAULT_RUNS = 3
RUN_TIMEOUT = int(os.environ.get("BENCH_TIMEOUT", "3600"))

README_BEGIN = "<!-- BENCH:RESULTS:BEGIN -->"
README_END = "<!-- BENCH:RESULTS:END -->"


# --------------------------------------------------------------------------
# prompt extraction
# --------------------------------------------------------------------------

def task_prompt() -> str:
    """The fenced block in TASK.md, before the 'why this task' section."""
    text = TASK_MD.read_text()
    head = text.split("## Why this task")[0]
    blocks = re.findall(r"```\n(.*?)```", head, re.DOTALL)
    if not blocks:
        sys.exit("could not find the prompt block in TASK.md")
    return blocks[0].strip()


def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", model.lower()).strip("-")


# --------------------------------------------------------------------------
# opencode invocation
# --------------------------------------------------------------------------

# Keep runs comparable: no external plugins, no ~/.claude/CLAUDE.md or
# .claude/skills leaking in, no autoupdate mid-benchmark.
CLEAN_ENV = {
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
}


def opencode(
    prompt: str,
    model: str,
    cwd: Path,
    *,
    json_events: bool = False,
    variant: str | None = None,
    auto: bool = True,
    stream_stderr: bool = False,
) -> tuple[str, str, int, float]:
    """Run opencode headless. Returns (stdout, stderr, returncode, seconds).

    With json_events=True, stdout is a stream of raw JSON events and stderr
    carries the logs; otherwise stdout is the formatted transcript.
    With stream_stderr=True, stderr is tee'd to the terminal in real time.
    """
    cmd = ["opencode", "run", "--pure", "--model", model, "--dir", str(cwd), "--print-logs"]
    if auto:
        cmd.append("--auto")          # otherwise the agent blocks on a permission prompt
    if json_events:
        cmd += ["--format", "json"]
    if variant:
        cmd += ["--variant", variant]  # provider-specific reasoning effort
    cmd.append(prompt)

    env = {**os.environ, **CLEAN_ENV}
    started = time.monotonic()
    try:
        if stream_stderr:
            import io
            import threading
            proc = subprocess.Popen(
                cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env,
            )
            err_buf = io.StringIO()

            def _tee_stderr():
                for line in proc.stderr:
                    sys.stderr.write(line)
                    sys.stderr.flush()
                    err_buf.write(line)

            t = threading.Thread(target=_tee_stderr, daemon=True)
            t.start()
            out = proc.stdout.read()
            t.join()
            proc.wait(timeout=RUN_TIMEOUT)
            err, code = err_buf.getvalue(), proc.returncode
        else:
            proc = subprocess.run(
                cmd, cwd=str(cwd), capture_output=True, text=True,
                timeout=RUN_TIMEOUT, env=env,
            )
            out, err, code = proc.stdout, proc.stderr, proc.returncode
    except FileNotFoundError:
        sys.exit("opencode not found on PATH")
    except subprocess.TimeoutExpired as exc:
        out = _text(exc.stdout)
        err = _text(exc.stderr) + f"\n[bench] timed out after {RUN_TIMEOUT}s\n"
        code = 124
    return out, err, code, time.monotonic() - started


def _text(blob) -> str:
    if blob is None:
        return ""
    return blob.decode(errors="replace") if isinstance(blob, bytes) else blob


def _opencode_json(args: list[str]) -> object | None:
    """Run an opencode subcommand that emits JSON on stdout."""
    try:
        proc = subprocess.run(
            ["opencode", *args], capture_output=True, text=True, timeout=120,
            env={**os.environ, **CLEAN_ENV},
        )
        return json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# --- token accounting -------------------------------------------------------
# opencode's AssistantMessage and StepFinishPart both carry
#   tokens: {input, output, reasoning, cache: {read, write}}
# plus a `cost`. A single assistant turn can contain many step-finish parts
# (one per tool-loop step), so step-finish is the additive unit; message-level
# totals are kept as a cross-check. Subagent work happens in child sessions,
# which are found via parentID and added in.

EMPTY = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}


def _flatten(tok: dict, cost: float = 0.0) -> dict:
    cache = tok.get("cache") or {}
    return {
        "input": tok.get("input", 0) or 0,
        "output": tok.get("output", 0) or 0,
        "reasoning": tok.get("reasoning", 0) or 0,
        "cache_read": cache.get("read", 0) or 0,
        "cache_write": cache.get("write", 0) or 0,
        "cost": cost or 0.0,
    }


def _add(a: dict, b: dict) -> dict:
    return {k: a.get(k, 0) + b.get(k, 0) for k in EMPTY}


def _finalise(steps: dict, messages: dict, session_ids: list[str]) -> dict:
    # Prefer the step-finish sum; fall back to message totals if the stream
    # carried no step-finish parts (some providers / older builds).
    chosen = steps if steps["input"] or steps["output"] else messages
    out = dict(chosen)
    out["total"] = out["input"] + out["output"] + out["reasoning"]
    out["billable_total"] = out["input"] + out["output"]
    out["source"] = "step-finish" if chosen is steps else "message"
    out["message_total_crosscheck"] = messages["input"] + messages["output"] + messages["reasoning"]
    out["sessions"] = session_ids
    return out


def _scan_events(objs) -> tuple[dict, dict, set[str]]:
    steps, messages, sessions = dict(EMPTY), dict(EMPTY), set()
    seen_parts, seen_msgs = set(), set()

    def visit(node):
        nonlocal steps, messages
        if isinstance(node, list):
            for v in node:
                visit(v)
            return
        if not isinstance(node, dict):
            return
        sid = node.get("sessionID")
        if isinstance(sid, str):
            sessions.add(sid)
        if node.get("type") == "step-finish" and isinstance(node.get("tokens"), dict):
            key = node.get("id") or id(node)
            if key not in seen_parts:
                seen_parts.add(key)
                steps = _add(steps, _flatten(node["tokens"], node.get("cost", 0.0)))
        elif node.get("role") == "assistant" and isinstance(node.get("tokens"), dict):
            key = node.get("id") or id(node)
            if key not in seen_msgs:
                seen_msgs.add(key)
                messages = _add(messages, _flatten(node["tokens"], node.get("cost", 0.0)))
        for v in node.values():
            visit(v)

    visit(objs)
    return steps, messages, sessions


def tokens_from_stream(stdout: str) -> tuple[dict, dict, set[str]]:
    """Parse the ndjson emitted by `opencode run --format json`."""
    objs = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                objs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return _scan_events(objs)


def tokens_from_export(session_ids: set[str]) -> tuple[dict, dict, set[str]]:
    """Cross-check via `opencode export`, following child (subagent) sessions."""
    all_sessions = set(session_ids)
    listing = _opencode_json(["session", "list", "--format", "json", "-n", "100"])
    if isinstance(listing, list):
        for _ in range(3):  # follow a few generations of subagents
            children = {
                s["id"] for s in listing
                if isinstance(s, dict) and s.get("parentID") in all_sessions
            }
            if children <= all_sessions:
                break
            all_sessions |= children

    steps, messages = dict(EMPTY), dict(EMPTY)
    for sid in sorted(all_sessions):
        doc = _opencode_json(["export", sid])
        if doc is None:
            continue
        s, m, _ = _scan_events(doc)
        steps, messages = _add(steps, s), _add(messages, m)
    return steps, messages, all_sessions


def collect_tokens(stdout: str) -> dict:
    steps, messages, sessions = tokens_from_stream(stdout)
    if not (steps["input"] or messages["input"]) or not sessions:
        e_steps, e_messages, sessions = tokens_from_export(sessions)
        steps, messages = e_steps, e_messages
    elif sessions:
        # The run stream misses child sessions on some builds; if export finds
        # more sessions than the stream did, trust export.
        e_steps, e_messages, all_sessions = tokens_from_export(sessions)
        if len(all_sessions) > len(sessions) and e_steps["input"] >= steps["input"]:
            steps, messages, sessions = e_steps, e_messages, all_sessions
    return _finalise(steps, messages, sorted(sessions))


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def git_init(work: Path) -> None:
    """One commit of pristine code, so the reviewer can `git diff` the agent's work."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "bench@local",
           "GIT_COMMITTER_NAME": "bench", "GIT_COMMITTER_EMAIL": "bench@local"}
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"],
                ["commit", "-q", "-m", "pristine notify-digest"]):
        subprocess.run(["git", *cmd], cwd=str(work), env=env,
                       capture_output=True, check=False)


def _do_run(n: int, total: int, label: str, args, prompt: str) -> None:
    """Execute a single numbered run and write its artefacts."""
    dest = RESULTS / label / f"run-{n}"
    shutil.rmtree(dest, ignore_errors=True)

    # Each run lives alone in its own temp tree: no sibling runs, no
    # results/ directory, nothing from this benchmark anywhere the agent
    # can reach by walking up.
    with tempfile.TemporaryDirectory(prefix=f"bench-{label}-{n}-") as tmp:
        work = Path(tmp) / "notify-digest"
        shutil.copytree(ORIGINAL, work)
        if not args.no_git:
            git_init(work)
        print(f"[bench] {label} run {n}/{total} in {work}")

        wall_start = time.time()
        stdout, stderr, code, seconds = opencode(
            prompt, args.model, work, json_events=True, variant=args.variant
        )
        tokens = collect_tokens(stdout)

        shutil.copytree(work, dest, ignore=shutil.ignore_patterns("__pycache__"))

    (dest / "_events.jsonl").write_text(stdout)
    (dest / "_agent.log").write_text(stderr)
    metrics = {
        "model": args.model,
        "variant": args.variant,
        "label": label,
        "run": n,
        "exit_code": code,
        "seconds": round(seconds, 1),
        "tokens": tokens,
        "started_at": datetime.fromtimestamp(wall_start, timezone.utc).isoformat(),
    }
    (dest / "_metrics.json").write_text(json.dumps(metrics, indent=2))
    print(
        f"[bench]   {label}/run-{n} exit {code}, {seconds/60:.1f} min, "
        f"in {tokens['input']} / out {tokens['output']} / reasoning {tokens['reasoning']} "
        f"/ cache r{tokens['cache_read']} w{tokens['cache_write']} "
        f"(total {tokens['total']}, ${tokens['cost']:.3f}, via {tokens['source']})"
    )


def cmd_run(args) -> int:
    label = args.label or slug(args.model + ("-" + args.variant if args.variant else ""))
    prompt = task_prompt()
    out_base = RESULTS / label
    out_base.mkdir(parents=True, exist_ok=True)

    pending = []
    for n in range(1, args.runs + 1):
        dest = out_base / f"run-{n}"
        if dest.exists() and not args.force:
            print(f"[bench] {label}/run-{n} exists, skipping (--force to redo)")
            continue
        pending.append(n)

    parallel = max(1, args.parallel)
    if parallel == 1:
        for n in pending:
            _do_run(n, args.runs, label, args, prompt)
    else:
        print(f"[bench] running {len(pending)} run(s) with parallelism={parallel}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {pool.submit(_do_run, n, args.runs, label, args, prompt): n for n in pending}
            for fut in concurrent.futures.as_completed(futures):
                exc = fut.exception()
                if exc:
                    print(f"[bench] run {futures[fut]} raised: {exc}")

    return 0


# --------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------

REVIEW_PROMPT = """You are reviewing one agent's attempt at a coding task, for a benchmark.

The pristine starting codebase is in `{original}`. The agent's finished work is
the directory you are running in. Diff them.

The agent was given exactly this prompt and nothing else — no clarification was
offered and any question it asked went unanswered:

    {prompt}

Score the result against the rubric in `{rubric}`, which is authoritative. Read
it before scoring. Do not invent categories or reweight them.

Read the actual code. Run it if that helps — the fixtures under `fixtures/` are
two snapshots of the same feeds taken at different times, and running against
snapshot-a and then snapshot-b is a reasonable way to check whether the
deduplication actually works. Do not modify the agent's code; if you need a
scratch copy, make one in /tmp.

Write your review to `_review.md` in this directory. Structure it as:

1. **Summary** — three sentences: what the agent did, and whether you would
   merge it.
2. **Per-category scoring** — one section per rubric category, each with the
   score, and specific file/line evidence for it. Quote the code you are
   scoring. Vague praise is worthless here; if you say the identity strategy is
   sound, show the function.
3. **What it missed** — the decisions the agent made silently or not at all.
4. **Bugs** — anything that is outright broken, with a reproduction.

Then, as the very last thing in the file, a fenced ```json block, exactly this
shape and nothing else in it:

```json
{{
  "identity_strategy": 0,
  "ambiguity_handling": 0,
  "failure_mode_reasoning": 0,
  "existing_code_respect": 0,
  "code_quality": 0,
  "documentation": 0,
  "total": 0,
  "would_merge": "yes" | "yes-with-fixes" | "no",
  "one_line": "under 15 words"
}}
```

Maxima are 10, 8, 8, 6, 4, 4 — total 40. Half points allowed. `total` must be
the sum of the six; check the arithmetic before you write it.
"""


def cmd_review(args) -> int:
    targets = [RESULTS / args.model] if args.model else sorted(p for p in RESULTS.iterdir() if p.is_dir())
    prompt_text = task_prompt()

    for label_dir in targets:
        if not label_dir.is_dir():
            sys.exit(f"no results at {label_dir}")
        for run_dir in sorted(label_dir.glob("run-*")):
            review_path = run_dir / "_review.md"
            if review_path.exists() and not args.force:
                print(f"[bench] {run_dir.name} already reviewed, skipping")
                continue
            print(f"[bench] reviewing {label_dir.name}/{run_dir.name} with {args.reviewer}")
            prompt = REVIEW_PROMPT.format(
                original=ORIGINAL,
                rubric=RUBRIC_MD,
                prompt=prompt_text.replace("\n", "\n    "),
            )
            out, err, code, seconds = opencode(prompt, args.reviewer, run_dir, stream_stderr=True)
            (run_dir / "_review.log").write_text(out + err)
            if not review_path.exists():
                print(f"[bench]   reviewer wrote no _review.md (exit {code}); see _review.log")
                continue
            scores = parse_scores(review_path.read_text())
            if scores is None:
                print("[bench]   could not parse the JSON score block")
                continue
            (run_dir / "_scores.json").write_text(json.dumps(scores, indent=2))
            print(f"[bench]   {scores['total']}/40 in {seconds/60:.1f} min")
    return 0


SCORE_KEYS = [
    ("identity_strategy", 10),
    ("ambiguity_handling", 8),
    ("failure_mode_reasoning", 8),
    ("existing_code_respect", 6),
    ("code_quality", 4),
    ("documentation", 4),
]


def parse_scores(text: str) -> dict | None:
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    for raw in reversed(blocks):
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not all(k in doc for k, _ in SCORE_KEYS):
            continue
        computed = sum(float(doc[k]) for k, _ in SCORE_KEYS)
        for key, cap in SCORE_KEYS:
            if not 0 <= float(doc[key]) <= cap:
                print(f"[bench]   warning: {key}={doc[key]} out of range 0..{cap}")
        if abs(computed - float(doc.get("total", -1))) > 0.01:
            print(f"[bench]   warning: reviewer total {doc.get('total')} != sum {computed}; using the sum")
        doc["total"] = computed
        return doc
    return None


# --------------------------------------------------------------------------
# gather
# --------------------------------------------------------------------------

def collect_summary() -> dict:
    models = []
    for label_dir in sorted(p for p in RESULTS.iterdir() if p.is_dir()):
        runs = []
        for run_dir in sorted(label_dir.glob("run-*")):
            entry = {"run": run_dir.name}
            for name, key in (("_metrics.json", "metrics"), ("_scores.json", "scores")):
                path = run_dir / name
                if path.exists():
                    entry[key] = json.loads(path.read_text())
            entry["review_path"] = str((run_dir / "_review.md").relative_to(ROOT))
            runs.append(entry)
        scored = [r["scores"]["total"] for r in runs if "scores" in r]
        times = [r["metrics"]["seconds"] for r in runs if "metrics" in r]
        tk = [r["metrics"]["tokens"] for r in runs if "metrics" in r]
        def mean(field):
            vals = [t.get(field) for t in tk if t.get(field)]
            return round(sum(vals) / len(vals)) if vals else None
        models.append({
            "label": label_dir.name,
            "model": next((r["metrics"]["model"] for r in runs if "metrics" in r), label_dir.name),
            "runs": runs,
            "mean_score": round(sum(scored) / len(scored), 2) if scored else None,
            "min_score": min(scored) if scored else None,
            "max_score": max(scored) if scored else None,
            "mean_seconds": round(sum(times) / len(times)) if times else None,
            "mean_tokens": {
                "input": mean("input"),
                "output": mean("output"),
                "reasoning": mean("reasoning"),
                "cache_read": mean("cache_read"),
                "cache_write": mean("cache_write"),
                "total": mean("total"),
            },
            "mean_cost_usd": round(sum(t["cost"] for t in tk) / len(tk), 4) if tk else None,
        })
    models.sort(key=lambda m: (m["mean_score"] is None, -(m["mean_score"] or 0)))
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "models": models}


GATHER_PROMPT = """You are updating the results section of the benchmark README.

`results/summary.json` in this directory has every model's per-run scores,
score spread, mean wall-clock seconds, and mean token usage, already sorted by
mean score. Each run also has a full written review at the `review_path` given
in the JSON. Read the reviews — the table is the least interesting part of your
job.

Rewrite everything in `README.md` between the markers
`{begin}` and `{end}`, leaving both markers and every line outside them exactly
as they are. Produce:

1. A ranking table: Model | Mean / 40 | Range | Runs | Mean time | Total tokens
   | Reasoning tokens | Cost. Range is `low–high` across the runs, or `—` for a
   single run. `mean_tokens.total` is input + output + reasoning; report
   reasoning separately as well, since a model spending half its budget on
   hidden reasoning is the interesting kind of expensive. A `null` is `n/a`,
   never a guess, and a reasoning count of 0 means the provider does not report
   it — say so rather than implying the model did not reason.

2. A per-category table: one row per model, one column per rubric category,
   showing the mean for each. This is where the interesting differences live.

3. **Notes** — a short paragraph per model. Say what it actually did: which
   identity strategy it picked, whether suppression was per-channel or global,
   where it put the sent-marker relative to delivery, what it left silent.
   Cite the specific choice, not the score. If a model's three runs disagreed
   substantially, that instability is the most important thing about it and
   should lead its paragraph.

4. **Patterns** — three to six bullets across all models. Which decisions were
   near-universal, which separated the top from the bottom, whether more tokens
   or more time bought better judgment.

Be direct about weak results. Do not pad, do not congratulate, and do not
describe a model's output as thoughtful unless the review quotes something
thoughtful. Every claim should trace to a review.
"""


def cmd_gather(args) -> int:
    summary = collect_summary()
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"[bench] wrote results/summary.json ({len(summary['models'])} models)")

    readme = ROOT / "README.md"
    text = readme.read_text()
    if README_BEGIN not in text or README_END not in text:
        sys.exit(f"README.md is missing the {README_BEGIN} / {README_END} markers")

    if args.dry_run:
        print(json.dumps(summary, indent=2))
        return 0

    prompt = GATHER_PROMPT.format(begin=README_BEGIN, end=README_END)
    out, err, code, seconds = opencode(prompt, args.gatherer, ROOT)
    (RESULTS / "_gather.log").write_text(out + err)
    after = readme.read_text()
    if after == text:
        print(f"[bench] gatherer did not change README.md (exit {code}); see results/_gather.log")
    else:
        print(f"[bench] README.md updated in {seconds/60:.1f} min")
    return 0


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bench")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run the task N times against a model")
    p.add_argument("--model", required=True, help="passed straight to opencode --model")
    p.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    p.add_argument("--label", help="results directory name (default: slug of --model)")
    p.add_argument("--variant", help="opencode --variant, i.e. provider reasoning effort")
    p.add_argument("--parallel", type=int, default=1, metavar="N",
                   help="run up to N benchmark runs concurrently (default: 1)")
    p.add_argument("--no-git", action="store_true", help="don't git init the work tree")
    p.add_argument("--force", action="store_true", help="redo runs that already exist")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("review", help="score runs with the reviewer model")
    p.add_argument("--model", help="results label to review (default: all)")
    p.add_argument("--reviewer", default=DEFAULT_REVIEWER)
    p.add_argument("--force", action="store_true", help="re-review already-reviewed runs")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("gather", help="aggregate scores into README.md")
    p.add_argument("--gatherer", default=DEFAULT_GATHERER)
    p.add_argument("--dry-run", action="store_true", help="write summary.json and print it, don't touch README")
    p.set_defaults(func=cmd_gather)

    args = ap.parse_args(argv)
    RESULTS.mkdir(exist_ok=True)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
