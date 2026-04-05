"""AutoResearch experiment tool handlers.

Implements the iterative optimization loop pattern inspired by Karpathy's
autoresearch: define a metric, iterate on changes, measure, keep/revert,
and accumulate learnings.

State is persisted in memory blocks as JSON (key: ``experiment:<id>``).
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import subprocess
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
_METRIC_CMD_TIMEOUT = 60
# Maximum allowed iterations as a hard cap
_HARD_MAX_ITERATIONS = 200


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

    # Handle benchmark mode
    mode = args.get("mode") or config.get("mode", "metric")
    config["mode"] = mode

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
        return {"error": f"All {num_samples} measurements failed", "errors": errors}

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
    metric_before = float(args["metric_before"])
    metric_after = float(args["metric_after"])
    improvement = _calc_improvement(metric_before, metric_after, state["direction"])
    cost_usd = float(args.get("cost_usd", 0))

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
    if verdict == "revert" and config.get("revert_command"):
        workspace = ctx.workspace or os.environ.get(
            "ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")
        )
        try:
            result = subprocess.run(
                config["revert_command"],
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=workspace,
            )
            revert_output = result.stdout.strip() or result.stderr.strip()
        except Exception as e:
            revert_output = f"Revert command failed: {e}"

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

    return response


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
