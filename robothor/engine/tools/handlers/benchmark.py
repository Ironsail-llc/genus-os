"""AutoAgent benchmark tool handlers.

Provides structured benchmark suites for evaluating agent harnesses.
Suites contain weighted tasks with expected-behavior criteria; scoring is
deterministic (pattern matching) to keep costs at zero for the evaluation
layer itself.  The aggregate score feeds into the experiment state machine
(mode=benchmark) so AutoAgent reuses the same hill-climbing loop as
AutoResearch.

State is persisted in memory blocks:
- Suite definitions: ``benchmark:<agent_id>:<suite_id>``
- Run results:       ``benchmark_run:<suite_id>:<tag>``
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

# Hard caps
_MAX_TASKS_PER_SUITE = 50
_MAX_COST_PER_TASK_USD = 0.50
_MAX_COST_PER_SUITE_USD = 5.00
_DEFAULT_TASK_MAX_COST = 0.15
_DEFAULT_SUITE_MAX_COST = 1.00


# ---------------------------------------------------------------------------
# Decorator + helpers  (same pattern as experiment.py)
# ---------------------------------------------------------------------------


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def _suite_block(agent_id: str, suite_id: str) -> str:
    return f"benchmark:{agent_id}:{suite_id}"


def _run_block(suite_id: str, tag: str) -> str:
    return f"benchmark_run:{suite_id}:{tag}"


def _load_block(key: str) -> dict[str, Any] | None:
    from robothor.memory.blocks import read_block

    result = read_block(key)
    if result.get("error"):
        return None
    try:
        parsed: dict[str, Any] = json.loads(result["content"])
        return parsed
    except (json.JSONDecodeError, KeyError):
        return None


def _save_block(key: str, data: dict[str, Any]) -> None:
    from robothor.memory.blocks import write_block

    data["updated_at"] = datetime.now(UTC).isoformat()
    write_block(key, json.dumps(data, indent=2, default=str))


def _resolve_path(path: str, workspace: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = Path(workspace) / p
    return p


def _validate_task(task: dict[str, Any]) -> str | None:
    """Return an error string if task is invalid, else None."""
    if not task.get("id"):
        return "task missing 'id'"
    if not task.get("prompt"):
        return f"task '{task['id']}' missing 'prompt'"
    category = task.get("category", "correctness")
    if category not in ("correctness", "safety", "efficiency", "tone"):
        return f"task '{task['id']}' has invalid category '{category}'"
    expected = task.get("expected", {})
    if not expected:
        return f"task '{task['id']}' missing 'expected' criteria"
    for field in ("must_contain", "must_not_contain"):
        for pattern in expected.get(field, []):
            try:
                re.compile(pattern)
            except re.error as exc:
                return f"task '{task['id']}' has invalid regex in {field}: {exc}"
    return None


def _score_task(output: str, expected: dict[str, Any], run_meta: dict[str, Any]) -> float:
    """Score an agent's output against expected criteria.  Returns 0.0-1.0.

    Scoring is deterministic (regex pattern matching + cost checks), no LLM.
    Each criterion is equally weighted within the task.
    """
    checks: list[bool] = []
    for p in expected.get("must_contain", []):
        try:
            checks.append(bool(re.search(p, output, re.IGNORECASE)))
        except re.error:
            checks.append(False)
    for p in expected.get("must_not_contain", []):
        try:
            checks.append(not bool(re.search(p, output, re.IGNORECASE)))
        except re.error:
            checks.append(False)

    # max_cost_usd: run cost must be within cap
    max_cost = expected.get("max_cost_usd")
    if max_cost is not None:
        checks.append(run_meta.get("total_cost_usd", 0) <= max_cost)

    # max_iterations: agent must finish within N steps
    max_iters = expected.get("max_iterations")
    if max_iters is not None:
        checks.append(run_meta.get("steps", 0) <= max_iters)

    if not checks:
        return 0.0

    return sum(checks) / len(checks)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


@_handler("benchmark_define")
async def _benchmark_define(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Define or update a benchmark suite for an agent."""
    agent_id = args.get("agent_id", "").strip()
    suite_id = args.get("suite_id", "").strip()
    if not agent_id or not suite_id:
        return {"error": "agent_id and suite_id are required"}

    # Load from YAML file or inline
    config_file = args.get("config_file")
    if config_file:
        path = _resolve_path(config_file, ctx.workspace)
        if not path.exists():
            return {"error": f"Config file not found: {path}"}
        suite_data = yaml.safe_load(path.read_text()) or {}
    else:
        suite_data = {
            "id": suite_id,
            "agent_id": agent_id,
            "description": args.get("description", ""),
            "max_cost_usd": args.get("max_cost_usd", _DEFAULT_SUITE_MAX_COST),
            "tasks": args.get("tasks", []),
        }

    # Normalise
    suite_data["id"] = suite_id
    suite_data["agent_id"] = agent_id

    # Validate tasks
    tasks = suite_data.get("tasks", [])
    if not tasks:
        return {"error": "Suite must have at least one task"}
    if len(tasks) > _MAX_TASKS_PER_SUITE:
        return {"error": f"Suite exceeds {_MAX_TASKS_PER_SUITE} task limit"}

    for task in tasks:
        err = _validate_task(task)
        if err:
            return {"error": err}
        # Enforce per-task cost cap
        expected = task.get("expected", {})
        task_max = expected.get("max_cost_usd", _DEFAULT_TASK_MAX_COST)
        expected["max_cost_usd"] = min(float(task_max), _MAX_COST_PER_TASK_USD)
        task["expected"] = expected
        # Default weight
        task.setdefault("weight", 1.0)
        task.setdefault("category", "correctness")

    # Cap suite cost
    suite_max = min(
        float(suite_data.get("max_cost_usd", _DEFAULT_SUITE_MAX_COST)), _MAX_COST_PER_SUITE_USD
    )
    suite_data["max_cost_usd"] = suite_max

    suite_data["created_at"] = suite_data.get("created_at", datetime.now(UTC).isoformat())

    block_key = _suite_block(agent_id, suite_id)
    _save_block(block_key, suite_data)

    return {
        "success": True,
        "agent_id": agent_id,
        "suite_id": suite_id,
        "task_count": len(tasks),
        "categories": sorted({t.get("category", "correctness") for t in tasks}),
        "max_cost_usd": suite_max,
        "message": f"Benchmark suite '{suite_id}' defined with {len(tasks)} tasks.",
    }


@_handler("benchmark_run")
async def _benchmark_run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Execute a benchmark suite against an agent and score the results.

    Each task spawns a sub-agent run.  Scoring is deterministic
    (pattern matching).  Returns per-task scores, per-category breakdown,
    and a weighted aggregate score (0.0-1.0).
    """
    from robothor.engine.tools.handlers.spawn import get_runner

    agent_id = args.get("agent_id", "").strip()
    suite_id = args.get("suite_id", "").strip()
    tag = args.get("tag", "").strip()
    if not agent_id or not suite_id or not tag:
        return {"error": "agent_id, suite_id, and tag are required"}

    # Load suite
    suite = _load_block(_suite_block(agent_id, suite_id))
    if suite is None:
        return {"error": f"Benchmark suite '{suite_id}' not found for agent '{agent_id}'"}

    # Check for existing run with this tag
    existing_run = _load_block(_run_block(suite_id, tag))
    if existing_run:
        return {"error": f"A run with tag '{tag}' already exists for suite '{suite_id}'"}

    # Filter to subset if requested
    task_ids = args.get("tasks")  # optional list of task IDs
    tasks = suite.get("tasks", [])
    if task_ids:
        tasks = [t for t in tasks if t["id"] in task_ids]
        if not tasks:
            return {"error": f"No matching tasks found for ids: {task_ids}"}

    # Get the runner for spawning sub-agent runs
    runner = get_runner()
    if runner is None:
        return {"error": "Runner not available — benchmark_run requires a running engine"}

    # Execute each task as a sub-agent run
    from robothor.engine.config import load_agent_config
    from robothor.engine.models import DeliveryMode, TriggerType

    results: list[dict[str, Any]] = []
    total_cost = 0.0
    suite_max_cost = suite.get("max_cost_usd", _DEFAULT_SUITE_MAX_COST)

    for task in tasks:
        # Cost guard
        if total_cost >= suite_max_cost:
            results.append(
                {
                    "task_id": task["id"],
                    "category": task.get("category", "correctness"),
                    "score": 0.0,
                    "skipped": True,
                    "reason": "suite cost budget exhausted",
                }
            )
            continue

        task_max_cost = task.get("expected", {}).get("max_cost_usd", _DEFAULT_TASK_MAX_COST)

        # Load and configure the target agent
        child_config = load_agent_config(agent_id, runner.config.manifest_dir)
        if child_config is None:
            results.append(
                {
                    "task_id": task["id"],
                    "category": task.get("category", "correctness"),
                    "score": 0.0,
                    "error": f"Agent config not found: {agent_id}",
                }
            )
            continue

        # Cap iterations and force silent delivery
        child_config.delivery_mode = DeliveryMode.NONE
        child_config.max_iterations = min(child_config.max_iterations, 15)
        child_config.max_cost_usd = task_max_cost

        try:
            run = await runner.execute(
                agent_id=agent_id,
                message=task["prompt"],
                trigger_type=TriggerType.SUB_AGENT,
                trigger_detail=f"benchmark:{suite_id}:{task['id']}",
                agent_config=child_config,
            )

            output = run.output_text or ""
            run_meta = {
                "total_cost_usd": run.total_cost_usd,
                "steps": len(run.steps),
                "status": run.status.value,
            }

            score = _score_task(output, task.get("expected", {}), run_meta)
            total_cost += run.total_cost_usd

            results.append(
                {
                    "task_id": task["id"],
                    "category": task.get("category", "correctness"),
                    "weight": task.get("weight", 1.0),
                    "score": round(score, 3),
                    "cost_usd": round(run.total_cost_usd, 4),
                    "steps": len(run.steps),
                    "status": run.status.value,
                    "output_preview": output[:200] if output else "",
                }
            )

        except Exception as e:
            logger.warning("Benchmark task %s failed: %s", task["id"], e)
            results.append(
                {
                    "task_id": task["id"],
                    "category": task.get("category", "correctness"),
                    "weight": task.get("weight", 1.0),
                    "score": 0.0,
                    "error": str(e),
                }
            )

    # Calculate aggregate scores
    scored = [r for r in results if not r.get("skipped")]
    if not scored:
        return {"error": "No tasks were scored"}

    # Weighted aggregate
    total_weight = sum(r.get("weight", 1.0) for r in scored)
    aggregate = (
        sum(r["score"] * r.get("weight", 1.0) for r in scored) / total_weight
        if total_weight > 0
        else 0.0
    )

    # Per-category breakdown
    categories: dict[str, list[float]] = {}
    for r in scored:
        cat = r.get("category", "correctness")
        categories.setdefault(cat, []).append(r["score"])

    category_scores = {cat: round(statistics.mean(scores), 3) for cat, scores in categories.items()}

    # Build run record
    run_record: dict[str, Any] = {
        "suite_id": suite_id,
        "agent_id": agent_id,
        "tag": tag,
        "timestamp": datetime.now(UTC).isoformat(),
        "total_cost_usd": round(total_cost, 4),
        "aggregate_score": round(aggregate, 3),
        "category_scores": category_scores,
        "task_results": results,
        "tasks_run": len(scored),
        "tasks_skipped": len(results) - len(scored),
    }

    _save_block(_run_block(suite_id, tag), run_record)

    # Write latest benchmark score for buddy RPG integration
    _save_block(
        f"agent_benchmark_latest:{agent_id}",
        {
            "agent_id": agent_id,
            "suite_id": suite_id,
            "tag": tag,
            "aggregate_score": round(aggregate, 3),
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )

    return {
        "success": True,
        "suite_id": suite_id,
        "tag": tag,
        "aggregate_score": round(aggregate, 3),
        "category_scores": category_scores,
        "total_cost_usd": round(total_cost, 4),
        "tasks_run": len(scored),
        "tasks_skipped": len(results) - len(scored),
        "task_results": results,
    }


@_handler("benchmark_compare")
async def _benchmark_compare(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Compare two benchmark runs and highlight regressions.

    Returns per-task deltas, per-category deltas, aggregate delta,
    and flags any safety-category regressions.
    """
    suite_id = args.get("suite_id", "").strip()
    run_a_tag = args.get("run_a", "").strip()
    run_b_tag = args.get("run_b", "").strip()
    if not suite_id or not run_a_tag or not run_b_tag:
        return {"error": "suite_id, run_a, and run_b are required"}

    run_a = _load_block(_run_block(suite_id, run_a_tag))
    run_b = _load_block(_run_block(suite_id, run_b_tag))
    if run_a is None:
        return {"error": f"Run '{run_a_tag}' not found for suite '{suite_id}'"}
    if run_b is None:
        return {"error": f"Run '{run_b_tag}' not found for suite '{suite_id}'"}

    # Build lookup: task_id -> result for each run
    a_by_id = {r["task_id"]: r for r in run_a.get("task_results", [])}
    b_by_id = {r["task_id"]: r for r in run_b.get("task_results", [])}

    all_task_ids = sorted(set(a_by_id) | set(b_by_id))

    task_deltas: list[dict[str, Any]] = []
    safety_regressions: list[dict[str, Any]] = []

    for tid in all_task_ids:
        a_result = a_by_id.get(tid)
        b_result = b_by_id.get(tid)

        a_score = a_result["score"] if a_result else None
        b_score = b_result["score"] if b_result else None

        delta = None
        if a_score is not None and b_score is not None:
            delta = round(b_score - a_score, 3)

        category = (b_result or a_result or {}).get("category", "correctness")

        entry = {
            "task_id": tid,
            "category": category,
            "score_a": a_score,
            "score_b": b_score,
            "delta": delta,
        }
        task_deltas.append(entry)

        # Flag safety regressions
        if category == "safety" and delta is not None and delta < 0:
            safety_regressions.append(entry)

    # Category-level deltas
    a_cats = run_a.get("category_scores", {})
    b_cats = run_b.get("category_scores", {})
    all_cats = sorted(set(a_cats) | set(b_cats))

    category_deltas: dict[str, dict[str, Any]] = {}
    for cat in all_cats:
        a_val = a_cats.get(cat)
        b_val = b_cats.get(cat)
        category_deltas[cat] = {
            "score_a": a_val,
            "score_b": b_val,
            "delta": round(b_val - a_val, 3) if a_val is not None and b_val is not None else None,
        }

    # Aggregate delta
    agg_a = run_a.get("aggregate_score", 0)
    agg_b = run_b.get("aggregate_score", 0)
    aggregate_delta = round(agg_b - agg_a, 3)

    return {
        "success": True,
        "suite_id": suite_id,
        "run_a": run_a_tag,
        "run_b": run_b_tag,
        "aggregate_score_a": agg_a,
        "aggregate_score_b": agg_b,
        "aggregate_delta": aggregate_delta,
        "category_deltas": category_deltas,
        "task_deltas": task_deltas,
        "safety_regressions": safety_regressions,
        "has_safety_regression": len(safety_regressions) > 0,
        "cost_a": run_a.get("total_cost_usd", 0),
        "cost_b": run_b.get("total_cost_usd", 0),
    }
