"""AutoResearch experiment tool handlers.

Implements the iterative optimization loop pattern inspired by Karpathy's
autoresearch: define a metric, iterate on changes, measure, keep/revert,
and accumulate learnings.

State is persisted in memory blocks as JSON (key: ``experiment:<id>``).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re as _re
import shutil
import statistics
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

# Subprocess timeout for metric commands (seconds)
_METRIC_CMD_TIMEOUT = 180
# Maximum allowed iterations as a hard cap
_HARD_MAX_ITERATIONS = 200
# Advisory locks older than this are treated as abandoned and reclaimable.
LOCK_STALENESS_HOURS = 24

# Valid file-path token: alphanumerics + dot/underscore/slash/hyphen. No spaces,
# parens, or newlines — rejects prose that leaks into search_space strings.
_FILE_PATH_RE = _re.compile(r"^[A-Za-z0-9._/-]+$")


def _now_iso() -> str:
    """Current time as ISO string — seam for tests."""
    return datetime.now(UTC).isoformat()


def _parse_search_space(search_space: str) -> list[str]:
    """Extract clean file-path tokens from a search_space string.

    Real-world search_space strings often include prose, parentheticals, and
    multiple lines (e.g. "brain/X.md (instruction file — simplify)"). This
    parser splits on commas AND whitespace-bounded tokens, strips parenthetical
    tails, and keeps only tokens that look like filesystem paths.
    """
    if not search_space:
        return []

    candidates: list[str] = []
    # Split on commas first, then fall back to whitespace/newline tokens.
    for chunk in search_space.replace("\n", ",").split(","):
        token = chunk.strip()
        if not token:
            continue
        # Strip leading list markers ("- path").
        token = token.lstrip("- \t")
        # Stop at the first parenthetical or whitespace — "path (desc)" → "path".
        token = token.split("(", 1)[0].strip()
        token = token.split()[0] if token.split() else ""
        # Require at least one "/" or "." — rules out bare words like "model",
        # "reduce", "timeouts" that got caught in the old comma-split. Reject
        # absolute paths and any token that contains `..` so a malicious
        # manifest cannot pollute unrelated `experiment_lock:*` keys or
        # clobber block storage outside the workspace.
        if (
            token
            and _FILE_PATH_RE.match(token)
            and ("/" in token or "." in token)
            and not token.startswith("/")
            and ".." not in token.split("/")
        ):
            candidates.append(token)

    # Dedup preserving order.
    seen: set[str] = set()
    result: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _suggest_measure_action(errors: list[str], metric_command: str) -> str:
    """Map measurement error patterns to concrete next steps."""
    joined = " ".join(errors).lower()
    if "timeoutexpired" in joined or "timeout" in joined:
        return (
            f"Metric command timed out ({_METRIC_CMD_TIMEOUT}s). Simplify the command, "
            "pre-warm caches, or raise _METRIC_CMD_TIMEOUT in experiment.py."
        )
    if "could not parse numeric metric" in joined:
        return (
            "Command ran but output is not numeric. Ensure the command's final line "
            "is a plain number (no units, labels, or JSON)."
        )
    if "no such file" in joined or "not found" in joined or "command not found" in joined:
        return f"Command references a missing file or binary. Verify: {metric_command!r}."
    if "permission denied" in joined:
        return "Permission denied — check execute bits on scripts the command invokes."
    return "Inspect errors[] above. Re-run manually to see full stderr."


def _lock_is_stale(last_written_at: str | None) -> bool:
    """True if a lock's last_written_at is older than LOCK_STALENESS_HOURS.

    "Now" is read from ``_now_iso()`` so tests can anchor time via that seam.
    """
    if not last_written_at:
        return False
    try:
        written = datetime.fromisoformat(last_written_at)
        now = datetime.fromisoformat(_now_iso())
    except (ValueError, TypeError):
        return False
    if written.tzinfo is None:
        # Naive timestamp is a bug signal (all writers emit tz-aware ISO)
        # — coerce to UTC so a one-off bad write doesn't wedge the lock.
        logger.warning("Lock timestamp %r was naive — assuming UTC", last_written_at)
        written = written.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    age_hours = (now - written).total_seconds() / 3600.0
    return age_hours >= LOCK_STALENESS_HOURS


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def _block_name(experiment_id: str) -> str:
    """Memory block key for an experiment."""
    return f"experiment:{experiment_id}"


def _load_state(experiment_id: str) -> dict[str, Any] | None:
    """Load experiment state from memory block."""
    from robothor.memory.blocks import read_block

    result = read_block(_block_name(experiment_id))
    if result.get("error"):
        return None
    try:
        parsed: dict[str, Any] = json.loads(result["content"])
        return parsed
    except (json.JSONDecodeError, KeyError):
        return None


def _save_state(experiment_id: str, state: dict[str, Any]) -> None:
    """Save experiment state to memory block."""
    from robothor.memory.blocks import write_block

    state["updated_at"] = datetime.now(UTC).isoformat()
    write_block(_block_name(experiment_id), json.dumps(state, indent=2, default=str))


def _resolve_path(path: str, workspace: str) -> Path:
    """Resolve a file path, expanding ~ and relative paths."""
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = Path(workspace) / p
    return p


def _run_metric_command(command: str, workspace: str) -> str:
    """Run a metric command and return stdout."""
    cwd = workspace or os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor"))
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=_METRIC_CMD_TIMEOUT,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Metric command failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _parse_metric_value(output: str) -> float:
    """Parse a numeric value from command output.

    Handles common formats: plain numbers, numbers with whitespace,
    last line containing a number.
    """
    # Try the whole output first
    try:
        return float(output.strip())
    except ValueError:
        pass

    # Try last non-empty line
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if lines:
        try:
            return float(lines[-1])
        except ValueError:
            pass

    raise ValueError(f"Could not parse numeric metric from output: {output!r}")


def _calc_improvement(baseline: float, current: float, direction: str) -> float:
    """Calculate improvement percentage.  Positive = better."""
    if baseline == 0:
        return 0.0
    if direction == "maximize":
        return ((current - baseline) / abs(baseline)) * 100
    else:  # minimize
        return ((baseline - current) / abs(baseline)) * 100


# ---------------------------------------------------------------------------
# Snapshot helpers for untracked-file revert support
# ---------------------------------------------------------------------------

# Directory used to store per-experiment snapshots of search-space files.
# Each experiment gets a subdirectory: /tmp/robothor_exp_snapshots/<experiment_id>/
_SNAPSHOT_BASE = Path(tempfile.gettempdir()) / "robothor_exp_snapshots"


def _snapshot_search_space(experiment_id: str, search_space: str, workspace: str) -> str | None:
    """Copy all search_space files to a temp snapshot dir before iteration.

    Returns the snapshot directory path on success, or None if nothing to snapshot.
    Called at experiment_create time so revert can always restore original state
    even for files not tracked by git.
    """
    files = _parse_search_space(search_space)
    if not files:
        return None

    snap_dir = _SNAPSHOT_BASE / experiment_id
    snap_dir.mkdir(parents=True, exist_ok=True)

    ws = Path(workspace)
    for rel_path in files:
        src = ws / rel_path
        if src.exists():
            dst = snap_dir / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    return str(snap_dir)


def _restore_snapshot(experiment_id: str, search_space: str, workspace: str) -> str:
    """Restore search_space files from snapshot dir.

    Returns a status message describing what was restored or why it failed.
    Called as fallback when revert_command fails or is absent.
    """
    files = _parse_search_space(search_space)
    if not files:
        return "No search_space files to restore."

    snap_dir = _SNAPSHOT_BASE / experiment_id
    if not snap_dir.exists():
        return f"Snapshot dir not found: {snap_dir}. Cannot restore."

    ws = Path(workspace)
    restored = []
    skipped = []
    for rel_path in files:
        src = snap_dir / rel_path
        if src.exists():
            dst = ws / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored.append(rel_path)
        else:
            skipped.append(rel_path)

    parts = []
    if restored:
        parts.append(f"Restored: {', '.join(restored)}")
    if skipped:
        parts.append(f"No snapshot for: {', '.join(skipped)}")
    return "; ".join(parts) if parts else "Nothing restored."


# ---------------------------------------------------------------------------
# Experiment guardrail checks
# ---------------------------------------------------------------------------


def _check_experiment_guardrails(
    config: dict[str, Any],
    changes: list[dict[str, Any]],
    revert_command: str | None = None,
) -> str | None:
    """Validate changes and commands against experiment guardrails.

    Returns an error message if a guardrail is violated, or None if all clear.
    """
    # Hard-deny (Phase 0a): independent of any per-experiment allowlist, an
    # agent may never write the benchmark suite it is graded against. Letting
    # the exam-taker edit the exam was the single worst reward-hack surface —
    # the cheapest way to raise the score was to weaken the test. Benchmark
    # suites are operator/benchmark-runner owned; edit them via benchmark_define
    # under human review, never through the experiment hill-climb.
    for change in changes:
        file_path = str(change.get("file", "")).replace("\\", "/")
        if "docs/benchmarks/" in file_path:
            return (
                f"Guardrail violation: '{file_path}' targets a benchmark suite. "
                "Agents may not edit the exam they are graded against (Phase 0a)."
            )

    guardrails = config.get("guardrails", [])
    if not guardrails:
        return None

    # Check write paths against allowlist
    if "write_path_restrict" in guardrails:
        allowed_paths = config.get("write_path_allowlist", [])
        if allowed_paths:
            for change in changes:
                file_path = change.get("file", "")
                if file_path and not any(fnmatch.fnmatch(file_path, pat) for pat in allowed_paths):
                    return (
                        f"Guardrail violation: file '{file_path}' is not in "
                        f"write_path_allowlist {allowed_paths}"
                    )

    # Check revert command against exec allowlist
    if revert_command and "exec_allowlist" in guardrails:
        exec_patterns = config.get("exec_allowlist", [])
        if exec_patterns and not any(_re.match(pat, revert_command) for pat in exec_patterns):
            return (
                f"Guardrail violation: revert_command '{revert_command}' does not "
                f"match any exec_allowlist pattern"
            )

    return None


# ---------------------------------------------------------------------------
# Advisory file locking for concurrent experiments
# ---------------------------------------------------------------------------


def _acquire_file_locks(experiment_id: str, search_space: str) -> str | None:
    """Check and acquire advisory locks for experiment search-space files.

    Returns an error message if any file is locked by another *active*
    experiment, or None if locks were acquired successfully. Locks older than
    LOCK_STALENESS_HOURS are considered abandoned and reclaimed.
    """
    from robothor.memory.blocks import read_block, write_block

    files = _parse_search_space(search_space)
    if not files:
        return None

    # Check for conflicts first
    for file_path in files:
        lock_key = f"experiment_lock:{file_path}"
        existing = read_block(lock_key)
        if not existing.get("error"):
            content = (existing.get("content") or "").strip()
            if content and content != experiment_id:
                if _lock_is_stale(existing.get("last_written_at")):
                    logger.warning(
                        "Reclaiming stale lock on %s (was held by %s, age > %dh)",
                        file_path,
                        content,
                        LOCK_STALENESS_HOURS,
                    )
                    continue
                return (
                    f"File '{file_path}' is locked by experiment '{content}'. "
                    f"Wait for it to complete or release locks manually."
                )

    # Acquire locks
    for file_path in files:
        lock_key = f"experiment_lock:{file_path}"
        write_block(lock_key, experiment_id)

    return None


def _release_file_locks(experiment_id: str, search_space: str) -> None:
    """Release advisory locks held by this experiment."""
    from robothor.memory.blocks import read_block, write_block

    files = _parse_search_space(search_space)
    for file_path in files:
        lock_key = f"experiment_lock:{file_path}"
        existing = read_block(lock_key)
        if not existing.get("error"):
            content = (existing.get("content") or "").strip()
            if content == experiment_id:
                write_block(lock_key, "")


# ---------------------------------------------------------------------------
# Benchmark mode helper
# ---------------------------------------------------------------------------


async def _measure_benchmark(
    experiment_id: str, state: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    """Run a benchmark suite and return the aggregate score as the metric value.

    Called by experiment_measure when mode=benchmark.
    """
    from robothor.engine.tools.handlers.benchmark import _benchmark_run

    config = state["config"]
    agent_id = config["benchmark_agent_id"]
    suite_id = config["benchmark_suite_id"]
    iteration = state["total_iterations"] + 1
    tag = f"exp-{experiment_id}-iter-{iteration}"

    bench_result = await _benchmark_run(
        {"agent_id": agent_id, "suite_id": suite_id, "tag": tag},
        ctx,
    )

    if bench_result.get("error"):
        return {"error": f"Benchmark run failed: {bench_result['error']}"}

    aggregate = bench_result["aggregate_score"]

    # If no baseline yet, set it
    just_set_baseline = state["baseline_value"] is None
    if just_set_baseline:
        state["baseline_value"] = aggregate
        state["current_best_value"] = aggregate
        _save_state(experiment_id, state)

    result: dict[str, Any] = {
        "experiment_id": experiment_id,
        "value": aggregate,
        "mode": "benchmark",
        "benchmark_tag": tag,
        "category_scores": bench_result.get("category_scores", {}),
        "tasks_run": bench_result.get("tasks_run", 0),
        "total_cost_usd": bench_result.get("total_cost_usd", 0),
        "timestamp": datetime.now(UTC).isoformat(),
    }

    result["baseline"] = state["baseline_value"]
    result["current_best"] = state["current_best_value"]
    result["vs_baseline_pct"] = round(
        _calc_improvement(state["baseline_value"], aggregate, state["direction"]), 2
    )
    if just_set_baseline:
        result["baseline_set"] = True
        result["message"] = f"Baseline established at {aggregate}"

    return result


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


@_handler("experiment_create")
async def _experiment_create(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create and initialise an experiment from a YAML config file or inline params."""
    experiment_id = args.get("experiment_id", "").strip()
    if not experiment_id:
        return {"error": "experiment_id is required"}

    # Check if already exists
    existing = _load_state(experiment_id)
    if existing:
        return {
            "error": f"Experiment '{experiment_id}' already exists (status: {existing.get('status')})"
        }

    # Load config — prefer config_file, fall back to inline params
    config: dict[str, Any] = {}
    config_file = args.get("config_file")
    if config_file:
        path = _resolve_path(config_file, ctx.workspace)
        if not path.exists():
            return {"error": f"Config file not found: {path}"}
        config = yaml.safe_load(path.read_text()) or {}
    else:
        # Build config from inline params
        config = {
            "metric_name": args.get("metric_name", experiment_id),
            "metric_command": args.get("metric_command", ""),
            "direction": args.get("direction", "maximize"),
            "search_space": args.get("search_space", ""),
            "max_iterations": args.get("max_iterations", 20),
            "min_improvement_pct": args.get("min_improvement_pct", 1.0),
            "measurement_samples": args.get("measurement_samples", 1),
            "measurement_delay_seconds": args.get("measurement_delay_seconds", 0),
            "revert_command": args.get("revert_command", ""),
            "guardrails": args.get("guardrails", []),
            "notify_on_improvement_pct": args.get("notify_on_improvement_pct", 10.0),
            "cost_budget_usd": args.get("cost_budget_usd", 2.0),
            "tags": args.get("tags", ["autoresearch"]),
        }

    # ── 2026-05-06 ABSOLUTE RULE ─────────────────────────────────────
    # Auto Researcher only optimizes benchmark_pass_rate. Metric mode is
    # blocked unless the caller explicitly opts in with operator_override=
    # "<reason>". See plan in-this-case-what-cozy-bunny.md.
    mode = args.get("mode") or config.get("mode", "benchmark")  # default flipped
    config["mode"] = mode

    operator_override = (args.get("operator_override") or "").strip()
    # Session-goal metrics ARE the operator's mandate, so they're exempt
    # from the operator_override requirement when running in metric mode.
    # Forbidden-token filter still applies below.
    session_goal_metrics = ("session_goal_alignment_score", "session_goal_progress")
    metric_name = (config.get("metric_name") or "").strip()
    is_session_goal_metric = metric_name in session_goal_metrics
    if mode != "benchmark" and not operator_override and not is_session_goal_metric:
        return {
            "error": (
                "Only benchmark-mode experiments are allowed (target: benchmark_pass_rate). "
                "Cost / token / latency / output_length optimization is forbidden by operator "
                "directive 2026-05-06. To run a metric-mode experiment with operator approval, "
                "set operator_override='<reason>'. Session-goal-driven metrics (e.g. "
                "session_goal_alignment_score) are exempt — they ARE the operator's mandate."
            ),
            "operator_override_required": True,
        }
    forbidden_metric_terms = (
        "cost",
        "tokens",
        "token",
        "latency",
        "duration",
        "p95",
        "output_length",
        "output_chars",
    )
    metric_name_lower = (config.get("metric_name") or "").lower()
    if mode == "benchmark" and any(term in metric_name_lower for term in forbidden_metric_terms):
        return {
            "error": (
                f"metric_name '{config.get('metric_name')}' contains a forbidden term "
                f"({', '.join(forbidden_metric_terms)}). Benchmark experiments must target "
                "the agent's job — rename the metric or pick a different framing."
            )
        }

    if mode == "benchmark":
        # Benchmark mode: metric comes from benchmark suite aggregate score
        benchmark_agent_id = args.get("benchmark_agent_id") or config.get("benchmark_agent_id", "")
        benchmark_suite_id = args.get("benchmark_suite_id") or config.get("benchmark_suite_id", "")
        if not benchmark_agent_id or not benchmark_suite_id:
            return {
                "error": "benchmark_agent_id and benchmark_suite_id are required for mode=benchmark"
            }
        config["benchmark_agent_id"] = benchmark_agent_id
        config["benchmark_suite_id"] = benchmark_suite_id
        # Auto-set metric fields for benchmark mode
        config.setdefault("metric_name", f"Benchmark score: {benchmark_suite_id}")
        config.setdefault("metric_command", "__benchmark__")  # sentinel — not a real command
        config.setdefault("direction", "maximize")
    else:
        # Validate required fields for metric mode
        if not config.get("metric_command"):
            return {"error": "metric_command is required (shell command that outputs a number)"}

    if config.get("direction") not in ("maximize", "minimize"):
        return {"error": "direction must be 'maximize' or 'minimize'"}

    # Cap max_iterations
    max_iter = min(int(config.get("max_iterations", 20)), _HARD_MAX_ITERATIONS)
    config["max_iterations"] = max_iter

    # Build initial state
    now = datetime.now(UTC).isoformat()
    state: dict[str, Any] = {
        "id": experiment_id,
        "metric_name": config.get("metric_name", experiment_id),
        "direction": config["direction"],
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "baseline_value": config.get("baseline_value"),
        "current_best_value": None,
        "current_best_iteration": None,
        "cumulative_improvement_pct": 0.0,
        "total_iterations": 0,
        "total_cost_usd": 0.0,
        "consecutive_no_improvement": 0,
        "config": config,
        "iterations": [],
        "learnings": {"positive": [], "negative": []},
    }

    # Acquire advisory file locks for search-space files
    search_space = config.get("search_space", "")
    if search_space:
        lock_error = _acquire_file_locks(experiment_id, search_space)
        if lock_error:
            return {"error": lock_error}

    # Snapshot search-space files for untracked-file revert support
    # (git checkout -- <file> fails on untracked files; snapshot-restore is the fallback)
    workspace_str = ctx.workspace or os.environ.get(
        "ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")
    )
    if search_space:
        try:
            _snapshot_search_space(experiment_id, search_space, workspace_str)
        except Exception as _snap_err:
            logger.warning("Snapshot failed for %s (non-fatal): %s", experiment_id, _snap_err)

    _save_state(experiment_id, state)

    return {
        "success": True,
        "experiment_id": experiment_id,
        "metric_name": config.get("metric_name", experiment_id),
        "direction": config["direction"],
        "max_iterations": max_iter,
        "status": "active",
        "message": "Experiment created. Use experiment_measure to establish baseline if not provided.",
    }


@_handler("experiment_measure")
async def _experiment_measure(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Run the metric command and return the measured value."""
    experiment_id = args.get("experiment_id", "").strip()
    if not experiment_id:
        return {"error": "experiment_id is required"}

    state = _load_state(experiment_id)
    if not state:
        return {"error": f"Experiment '{experiment_id}' not found"}
    if state["status"] not in ("active", "paused"):
        return {"error": f"Experiment is {state['status']}, cannot measure"}

    config = state["config"]
    mode = config.get("mode", "metric")

    if mode == "benchmark":
        # Benchmark mode: run the benchmark suite and use aggregate score
        return await _measure_benchmark(experiment_id, state, ctx)

    metric_command = config["metric_command"]
    num_samples = args.get("samples") or config.get("measurement_samples", 1)
    num_samples = max(1, min(int(num_samples), 10))  # cap at 10

    workspace = ctx.workspace or os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor"))
    samples: list[float] = []
    errors: list[str] = []

    for i in range(num_samples):
        try:
            output = _run_metric_command(metric_command, workspace)
            value = _parse_metric_value(output)
            samples.append(value)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            errors.append(f"Sample {i + 1}: {e}")

    if not samples:
        return {
            "error": f"All {num_samples} measurements failed",
            "errors": errors,
            "suggested_action": _suggest_measure_action(errors, metric_command),
        }

    avg_value = statistics.mean(samples)
    result: dict[str, Any] = {
        "experiment_id": experiment_id,
        "value": round(avg_value, 6),
        "samples": [round(s, 6) for s in samples],
        "num_samples": len(samples),
        "timestamp": datetime.now(UTC).isoformat(),
    }

    if errors:
        result["warnings"] = errors

    # If no baseline yet, set it
    if state["baseline_value"] is None:
        state["baseline_value"] = avg_value
        state["current_best_value"] = avg_value
        _save_state(experiment_id, state)
        result["baseline_set"] = True
        result["message"] = f"Baseline established at {avg_value}"

    # Add context
    if state["baseline_value"] is not None:
        result["baseline"] = state["baseline_value"]
        result["current_best"] = state["current_best_value"]
        result["vs_baseline_pct"] = round(
            _calc_improvement(state["baseline_value"], avg_value, state["direction"]), 2
        )

    return result


@_handler("experiment_commit")
async def _experiment_commit(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Record an iteration outcome — keep the change or revert it."""
    experiment_id = args.get("experiment_id", "").strip()
    if not experiment_id:
        return {"error": "experiment_id is required"}

    state = _load_state(experiment_id)
    if not state:
        return {"error": f"Experiment '{experiment_id}' not found"}
    if state["status"] != "active":
        return {"error": f"Experiment is {state['status']}, cannot commit"}

    # Validate required fields
    required = ["hypothesis", "changes", "metric_before", "metric_after", "verdict", "learnings"]
    missing = [f for f in required if f not in args or args[f] is None]
    if missing:
        return {"error": f"Missing required fields: {', '.join(missing)}"}

    verdict = args["verdict"]
    if verdict not in ("keep", "revert"):
        return {"error": "verdict must be 'keep' or 'revert'"}

    config = state["config"]

    # Enforce experiment guardrails before proceeding
    guardrail_error = _check_experiment_guardrails(
        config,
        args.get("changes", []),
        revert_command=config.get("revert_command") if verdict == "revert" else None,
    )
    if guardrail_error:
        return {"error": guardrail_error, "guardrail_violation": True}

    metric_before = float(args["metric_before"])
    metric_after = float(args["metric_after"])
    improvement = _calc_improvement(metric_before, metric_after, state["direction"])
    cost_usd = float(args.get("cost_usd", 0))

    # ── 2026-05-06: benchmark-mode regression guard ───────────────────
    # When the experiment is benchmark-driven and the agent claims to keep
    # a change, force-revert if any case that was passing in iteration 1
    # (baseline) is now failing in the latest iteration. Aggregate-level
    # gains can mask per-case regressions; we explicitly forbid that.
    forced_revert_reason: str | None = None
    if verdict == "keep" and config.get("mode") == "benchmark":
        try:
            from robothor.engine.tools.handlers.benchmark import (
                _load_block as _load_bench_block,
            )
            from robothor.engine.tools.handlers.benchmark import (
                _run_block as _bench_run_block,
            )

            suite_id = config.get("benchmark_suite_id", "")
            current_iter = state["total_iterations"] + 1
            baseline_tag = f"exp-{experiment_id}-iter-1"
            current_tag = f"exp-{experiment_id}-iter-{current_iter}"
            baseline_run = _load_bench_block(_bench_run_block(suite_id, baseline_tag))
            current_run = _load_bench_block(_bench_run_block(suite_id, current_tag))
            if baseline_run and current_run:
                base_results = {r["task_id"]: r for r in baseline_run.get("task_results", [])}
                cur_results = {r["task_id"]: r for r in current_run.get("task_results", [])}
                regressed_cases: list[str] = []
                for tid, b in base_results.items():
                    base_score = float(b.get("score", 0))
                    cur = cur_results.get(tid)
                    cur_score = float(cur.get("score", 0)) if cur else 0.0
                    if base_score >= 0.7 and cur_score < 0.7:
                        regressed_cases.append(tid)
                if regressed_cases:
                    verdict = "revert"
                    forced_revert_reason = (
                        f"benchmark regression guard tripped: "
                        f"{', '.join(regressed_cases)} were passing at baseline "
                        f"and are now failing — see plan 2026-05-06"
                    )
                    iteration_will_revert_msg = forced_revert_reason  # noqa: F841 (informational)
        except Exception as exc:  # pragma: no cover (best-effort guard)
            logger.warning("Regression guard could not run for %s: %s", experiment_id, exc)

    # Build iteration record
    iteration_number = state["total_iterations"] + 1
    iteration: dict[str, Any] = {
        "number": iteration_number,
        "timestamp": datetime.now(UTC).isoformat(),
        "hypothesis": args["hypothesis"],
        "changes": args["changes"],  # list of {file, description}
        "metric_before": metric_before,
        "metric_after": metric_after,
        "improvement_pct": round(improvement, 2),
        "verdict": verdict,
        "learnings": args["learnings"],
        "cost_usd": cost_usd,
    }

    # Update state
    state["iterations"].append(iteration)
    state["total_iterations"] = iteration_number
    state["total_cost_usd"] = round(state["total_cost_usd"] + cost_usd, 4)

    # Accumulate learnings
    learnings_text = args["learnings"]
    if verdict == "keep":
        state["learnings"]["positive"].append(
            f"Iter {iteration_number}: {learnings_text} (+{improvement:.1f}%)"
        )
        state["consecutive_no_improvement"] = 0
    else:
        state["learnings"]["negative"].append(
            f"Iter {iteration_number}: {learnings_text} ({improvement:+.1f}%)"
        )
        state["consecutive_no_improvement"] = state.get("consecutive_no_improvement", 0) + 1

    # Update best value
    if verdict == "keep":
        baseline = state["baseline_value"] or metric_before
        if state["current_best_value"] is None or (
            (state["direction"] == "maximize" and metric_after > state["current_best_value"])
            or (state["direction"] == "minimize" and metric_after < state["current_best_value"])
        ):
            state["current_best_value"] = metric_after
            state["current_best_iteration"] = iteration_number
            state["cumulative_improvement_pct"] = round(
                _calc_improvement(baseline, metric_after, state["direction"]), 2
            )

    # Execute revert command if verdict is revert
    revert_output = None
    if verdict == "revert":
        workspace = ctx.workspace or os.environ.get(
            "ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")
        )
        revert_cmd = config.get("revert_command")
        if revert_cmd:
            try:
                result = subprocess.run(
                    revert_cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=workspace,
                )
                revert_output = result.stdout.strip() or result.stderr.strip()
                # If revert_command failed (non-zero exit or error output containing
                # "error" / "pathspec" / "did not match"), fall back to snapshot restore.
                if result.returncode != 0 or any(
                    kw in (revert_output or "").lower()
                    for kw in ("error", "pathspec", "did not match", "fatal")
                ):
                    logger.warning(
                        "revert_command failed for %s (rc=%d, out=%r); falling back to snapshot restore",
                        experiment_id,
                        result.returncode,
                        revert_output,
                    )
                    search_space = config.get("search_space", "")
                    if search_space:
                        snap_result = _restore_snapshot(experiment_id, search_space, workspace)
                        revert_output = f"revert_command failed ({revert_output}); snapshot restore: {snap_result}"
            except Exception as e:
                revert_output = f"Revert command failed: {e}"
                # Fall back to snapshot restore
                search_space = config.get("search_space", "")
                if search_space:
                    try:
                        snap_result = _restore_snapshot(experiment_id, search_space, workspace)
                        revert_output += f"; snapshot restore: {snap_result}"
                    except Exception as snap_err:
                        revert_output += f"; snapshot restore also failed: {snap_err}"
        else:
            # No revert_command — restore from snapshot directly
            search_space = config.get("search_space", "")
            if search_space:
                workspace_str = workspace
                try:
                    revert_output = _restore_snapshot(experiment_id, search_space, workspace_str)
                except Exception as snap_err:
                    revert_output = f"Snapshot restore failed: {snap_err}"

    # Check termination conditions
    termination_reason = None

    if iteration_number >= config.get("max_iterations", 20):
        termination_reason = "max_iterations_reached"
        state["status"] = "completed"

    if config.get("cost_budget_usd") and state["total_cost_usd"] >= config["cost_budget_usd"]:
        termination_reason = "cost_budget_exhausted"
        state["status"] = "completed"

    # Degradation circuit breaker: >10% worse than baseline
    if state["baseline_value"] is not None and verdict == "keep":
        degradation = _calc_improvement(state["baseline_value"], metric_after, state["direction"])
        if degradation < -10.0:
            termination_reason = "degradation_circuit_breaker"
            state["status"] = "paused"

    # Convergence: 3 consecutive no-improvement
    if state.get("consecutive_no_improvement", 0) >= 3:
        # Don't terminate, but flag it — agent instruction file handles strategy switch
        pass

    _save_state(experiment_id, state)

    # Release file locks when experiment terminates
    if state["status"] in ("completed", "paused"):
        search_space = config.get("search_space", "")
        if search_space:
            _release_file_locks(experiment_id, search_space)

    response: dict[str, Any] = {
        "success": True,
        "experiment_id": experiment_id,
        "iteration": iteration_number,
        "verdict": verdict,
        "improvement_pct": round(improvement, 2),
        "cumulative_improvement_pct": state["cumulative_improvement_pct"],
        "current_best_value": state["current_best_value"],
        "total_iterations": iteration_number,
        "status": state["status"],
    }

    if revert_output:
        response["revert_output"] = revert_output
    if termination_reason:
        response["termination_reason"] = termination_reason
    if state.get("consecutive_no_improvement", 0) >= 3:
        response["warning"] = (
            "3+ consecutive iterations with no improvement — consider switching strategy"
        )

    # Check if we should announce
    notify_threshold = config.get("notify_on_improvement_pct", 10.0)
    if verdict == "keep" and state["cumulative_improvement_pct"] >= notify_threshold:
        response["announce"] = True
        response["announcement"] = (
            f"Experiment '{experiment_id}': {state['metric_name']} improved "
            f"{state['cumulative_improvement_pct']:.1f}% from baseline "
            f"({state['baseline_value']} -> {state['current_best_value']}) "
            f"after {iteration_number} iterations"
        )

    # Live-goal verification: when a benchmark experiment terminates with a
    # positive cumulative improvement, enqueue a 7-day follow-up for
    # buddy-grader to confirm the target agent's real goal metric moved.
    # Benchmark wins can overfit the scorer; this is the ground-truth check.
    if (
        verdict == "keep"
        and state["status"] == "completed"
        and config.get("mode") == "benchmark"
        and config.get("benchmark_agent_id")
        and state.get("cumulative_improvement_pct", 0) > 0
    ):
        _enqueue_live_goal_verification(experiment_id, state, iteration_number)

    return response


def _enqueue_live_goal_verification(
    experiment_id: str, state: dict[str, Any], iteration_number: int
) -> None:
    """Schedule a 7-day live-goal check for a shipped benchmark win.

    Snapshots the target agent's current goal metrics into the task body so the
    grader can compute a before/after delta without needing to reconstruct the
    pre-ship window. Failures are swallowed — a missing verification task is a
    warning, not a reason to fail the experiment commit.
    """
    try:
        from datetime import timedelta

        from robothor.crm.dal import create_task
        from robothor.engine.goals import compute_goal_metrics

        config = state["config"]
        agent_id = config["benchmark_agent_id"]
        snapshot = compute_goal_metrics(agent_id, window_days=7)
        follow_up_at = datetime.now(UTC) + timedelta(days=7)

        body = (
            f"Benchmark experiment '{experiment_id}' closed with "
            f"{state['cumulative_improvement_pct']:+.2f}% vs baseline "
            f"({state['baseline_value']} -> {state['current_best_value']}, "
            f"{iteration_number} iterations).\n\n"
            "Verify that the agent's LIVE goal metrics moved in the same direction "
            "over the 7 days since the ship.\n\n"
            "## Pre-ship goal metrics snapshot (7-day window before ship)\n"
            "```json\n"
            f"{json.dumps(snapshot, indent=2, default=str)}\n"
            "```\n\n"
            "Procedure:\n"
            "1. Call compute_goal_metrics(agent_id, window_days=7) now.\n"
            "2. Compare against the snapshot above.\n"
            "3. Append one line to autoagent_learnings: "
            "`YYYY-MM-DD | {agent_id} | {experiment_id}: benchmark +X% → live {metric} "
            "Δ {pre → post}`.\n"
            "4. If live metrics regressed or are flat, file a follow-up task tagged "
            "`benchmark-overfit` so auto-agent re-evaluates the harness change."
        )
        create_task(
            title=f"Verify live-goal impact: {agent_id} after {experiment_id}",
            body=body,
            assigned_to_agent="auto-agent",
            created_by_agent="experiment_commit",
            priority="normal",
            tags=["live-goal-verify", agent_id, experiment_id],
            follow_up_at=follow_up_at,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not enqueue live-goal verification for %s: %s",
            experiment_id,
            exc,
        )


@_handler("experiment_status")
async def _experiment_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Return the current state of an experiment."""
    experiment_id = args.get("experiment_id", "").strip()
    if not experiment_id:
        return {"error": "experiment_id is required"}

    state = _load_state(experiment_id)
    if not state:
        return {"error": f"Experiment '{experiment_id}' not found"}

    include_iterations = args.get("include_iterations", False)

    result: dict[str, Any] = {
        "experiment_id": state["id"],
        "metric_name": state["metric_name"],
        "direction": state["direction"],
        "status": state["status"],
        "baseline_value": state["baseline_value"],
        "current_best_value": state["current_best_value"],
        "current_best_iteration": state["current_best_iteration"],
        "cumulative_improvement_pct": state["cumulative_improvement_pct"],
        "total_iterations": state["total_iterations"],
        "total_cost_usd": state["total_cost_usd"],
        "consecutive_no_improvement": state.get("consecutive_no_improvement", 0),
        "created_at": state["created_at"],
        "updated_at": state.get("updated_at"),
        "learnings": state["learnings"],
        "search_space": state["config"].get("search_space", ""),
        "max_iterations": state["config"].get("max_iterations"),
        "cost_budget_usd": state["config"].get("cost_budget_usd"),
    }

    if include_iterations:
        result["iterations"] = state["iterations"]

    return result
