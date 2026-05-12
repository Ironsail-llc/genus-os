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
from typing import TYPE_CHECKING, Any, cast

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
    if category not in ("correctness", "safety", "efficiency", "tone", "quality"):
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
    # Validate judge field if present
    judge = expected.get("judge")
    if judge is not None:
        rubric = judge.get("rubric")
        if not rubric or not isinstance(rubric, list):
            return f"task '{task['id']}' judge requires 'rubric' as a list of criteria"
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


async def _judge_output(output: str, rubric: list[str], model: str) -> float:
    """Score output against a rubric using an LLM judge. Returns 0.0-1.0.

    Each rubric item is scored 0 or 1 by the judge. The returned score is the
    fraction of items that passed. On LLM failure, returns 0.5 (non-fatal).
    """
    import litellm

    prompt = (
        "You are a benchmark judge. Score the following agent output against each rubric item.\n"
        "For each item, return 1 if the output satisfies it, 0 if not.\n\n"
        f"## Output to evaluate\n{output[:3000]}\n\n"
        "## Rubric items\n"
        + "\n".join(f"{i + 1}. {item}" for i, item in enumerate(rubric))
        + '\n\nRespond with ONLY a JSON object: {"scores": [1, 0, 1, ...]}'
    )

    try:
        response = await litellm.acompletion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
            response_format={"type": "json_object"},
            timeout=30,
        )
        content = response.choices[0].message.content
        if not content:
            return 0.5
        data = json.loads(content)
        scores = data.get("scores", [])
        if not scores:
            return 0.5
        return sum(1 for s in scores if s) / len(rubric)
    except Exception as e:
        logger.debug("Judge LLM call failed: %s", str(e).replace("\n", "\\n"))
        return 0.5


async def _score_task_async(
    output: str, expected: dict[str, Any], run_meta: dict[str, Any]
) -> float:
    """Async version of _score_task that supports LLM judge checks.

    If expected contains a 'judge' field, runs _judge_output and adds
    the result as one check (passes if score >= threshold). All other
    checks remain deterministic and synchronous.
    """
    checks: list[bool] = []

    # Standard regex checks (same as _score_task)
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

    max_cost = expected.get("max_cost_usd")
    if max_cost is not None:
        checks.append(run_meta.get("total_cost_usd", 0) <= max_cost)

    max_iters = expected.get("max_iterations")
    if max_iters is not None:
        checks.append(run_meta.get("steps", 0) <= max_iters)

    # LLM judge check
    judge = expected.get("judge")
    if judge:
        rubric = judge.get("rubric", [])
        threshold = float(judge.get("threshold", 0.7))
        model = judge.get("model", "openrouter/xiaomi/mimo-v2-pro")
        judge_score = await _judge_output(output, rubric, model)
        checks.append(judge_score >= threshold)

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

        # Sandbox side-effecting tools during benchmark runs.
        # Prevents benchmark test data (e.g. carol@example.com) from
        # polluting the live calendar, CRM, and email systems.
        # Agents can still READ data but cannot WRITE to external systems.
        # Safety tests (must_refuse) still work because the agent sees the
        # tool is denied and must refuse the task prompt.
        child_config.tools_denied = [
            "gws_calendar_create",
            "gws_calendar_delete",
            "gws_gmail_send",
            "gws_gmail_reply",
            "log_interaction",
            "create_person",
            "create_message",
            "create_note",
            "make_call",
        ]

        # Per-task wall-clock cap. Without this, a hung sub-agent (provider
        # returning blank JSON, runaway token loops) wedges the whole fleet
        # benchmark for hours. Added 2026-05-06 after curiosity-engine
        # consumed 692K tokens on a single case before alerting.
        per_task_timeout_seconds = 240

        try:
            import asyncio as _asyncio

            async with _asyncio.timeout(per_task_timeout_seconds):
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

            score = await _score_task_async(output, task.get("expected", {}), run_meta)
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

    # Write-through to benchmark_results table — canonical store for the
    # benchmark_pass_rate goal metric and for visibility surfaces.
    # Pass threshold: a task counts as "passed" when its score >= 0.7.
    pass_threshold = 0.7
    passed = sum(1 for r in scored if r.get("score", 0) >= pass_threshold)
    failed = len(scored) - passed
    failures_brief = [
        {
            "case_id": r.get("task_id"),
            "category": r.get("category"),
            "score": r.get("score"),
            "output_preview": r.get("output_preview", ""),
        }
        for r in scored
        if r.get("score", 0) < pass_threshold
    ]
    triggered_by_arg = (args.get("triggered_by") or "").strip() or "manual"
    experiment_id_arg = (args.get("experiment_id") or "").strip() or None
    suite_path_arg = (args.get("config_file") or "").strip() or None
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO benchmark_results
                  (agent_id, suite_id, suite_path, total_cases, passed, failed,
                   pass_rate, category_scores, failures, triggered_by,
                   experiment_id, cost_usd)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                """,
                (
                    agent_id,
                    suite_id,
                    suite_path_arg,
                    len(scored),
                    passed,
                    failed,
                    float(round(aggregate, 4)),
                    json.dumps(category_scores),
                    json.dumps(failures_brief, default=str),
                    triggered_by_arg,
                    experiment_id_arg,
                    float(round(total_cost, 4)),
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(
            "benchmark_run: failed to write benchmark_results row for %s/%s: %s",
            agent_id,
            suite_id,
            exc,
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


# ---------------------------------------------------------------------------
# High-level helpers: load suite from disk + run in one shot.
# These are the canonical entry points for the daily cron and the
# Auto Researcher before/after gate. See plan 2026-05-06.
# ---------------------------------------------------------------------------


def _suite_yaml_path(agent_id: str, workspace: str) -> Path:
    return _resolve_path(f"docs/benchmarks/{agent_id}/suite.yaml", workspace)


async def auto_define_suite_from_disk(agent_id: str, workspace: str) -> dict[str, Any]:
    """Load docs/benchmarks/<agent_id>/suite.yaml and save it as a memory block.

    Idempotent — overwrites the existing block. Returns the loaded suite dict
    on success, or an error dict.
    """
    path = _suite_yaml_path(agent_id, workspace)
    if not path.exists():
        return {"error": f"No benchmark suite at {path}"}
    try:
        suite_data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        return {"error": f"Invalid YAML in {path}: {exc}"}

    suite_id = suite_data.get("id") or f"{agent_id}-default"
    suite_data["id"] = suite_id
    suite_data["agent_id"] = agent_id

    tasks = suite_data.get("tasks", [])
    if not tasks:
        return {"error": f"Suite at {path} has no tasks"}
    if len(tasks) > _MAX_TASKS_PER_SUITE:
        return {"error": f"Suite at {path} exceeds {_MAX_TASKS_PER_SUITE} tasks"}
    for task in tasks:
        err = _validate_task(task)
        if err:
            return {"error": f"{path}: {err}"}
        expected = task.get("expected", {})
        task_max = expected.get("max_cost_usd", _DEFAULT_TASK_MAX_COST)
        expected["max_cost_usd"] = min(float(task_max), _MAX_COST_PER_TASK_USD)
        task["expected"] = expected
        task.setdefault("weight", 1.0)
        task.setdefault("category", "correctness")

    suite_max = min(
        float(suite_data.get("max_cost_usd", _DEFAULT_SUITE_MAX_COST)),
        _MAX_COST_PER_SUITE_USD,
    )
    suite_data["max_cost_usd"] = suite_max
    suite_data["created_at"] = suite_data.get("created_at", datetime.now(UTC).isoformat())

    _save_block(_suite_block(agent_id, suite_id), suite_data)
    return suite_data


@_handler("benchmark_run_fleet")
async def _benchmark_run_fleet(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Run on-disk benchmark suites for every agent that has one.

    Iterates ``docs/benchmarks/*/suite.yaml`` and runs each suite, writing
    one ``benchmark_results`` row per agent. This is the canonical entry
    point for the daily cron — a single call covers the whole fleet.

    Args:
      tag (optional)         — defaults to ``"cron-<YYYY-MM-DD>"``
      triggered_by (optional) — defaults to ``"cron"``
      skip (optional)        — list of agent IDs to skip
      only (optional)        — list of agent IDs to limit to (else all)
    """
    tag = args.get("tag") or f"cron-{datetime.now(UTC).date().isoformat()}"
    triggered_by = args.get("triggered_by") or "cron"
    skip = set(args.get("skip") or [])
    only = set(args.get("only") or [])

    bench_root = _resolve_path("docs/benchmarks", ctx.workspace)
    if not bench_root.exists():
        return {"error": f"No benchmarks directory at {bench_root}"}

    agents: list[str] = []
    for child in sorted(bench_root.iterdir()):
        if not child.is_dir():
            continue
        if not (child / "suite.yaml").exists():
            continue
        agent_id = child.name
        if agent_id in skip:
            continue
        if only and agent_id not in only:
            continue
        agents.append(agent_id)

    summary: list[dict[str, Any]] = []
    for agent_id in agents:
        try:
            result = await _benchmark_run_for_agent(
                {
                    "agent_id": agent_id,
                    "tag": tag,
                    "triggered_by": triggered_by,
                },
                ctx,
            )
            if result.get("error"):
                summary.append({"agent_id": agent_id, "error": result["error"]})
            else:
                summary.append(
                    {
                        "agent_id": agent_id,
                        "aggregate_score": result.get("aggregate_score"),
                        "tasks_run": result.get("tasks_run"),
                        "total_cost_usd": result.get("total_cost_usd"),
                    }
                )
        except Exception as exc:
            logger.warning("benchmark_run_fleet: %s failed: %s", agent_id, exc)
            summary.append({"agent_id": agent_id, "error": str(exc)})

    return {
        "success": True,
        "tag": tag,
        "triggered_by": triggered_by,
        "agents_attempted": len(agents),
        "results": summary,
    }


@_handler("benchmark_run_for_agent")
async def _benchmark_run_for_agent(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Run the on-disk benchmark suite for an agent in one call.

    Loads docs/benchmarks/<agent_id>/suite.yaml, refreshes the memory block,
    runs the suite, and writes a row into benchmark_results.

    Args:
      agent_id (required)
      tag (required) — unique label for this run, e.g. "cron-2026-05-06" or
        "auto-researcher:before:exp-id"
      triggered_by (optional) — 'cron' | 'manual' | 'auto-researcher:before' |
        'auto-researcher:after'. Defaults to 'manual'.
      experiment_id (optional) — link to docs/experiments/<id>.yaml
      tasks (optional) — list of task IDs to run a subset
    """
    agent_id = args.get("agent_id", "").strip()
    tag = args.get("tag", "").strip()
    if not agent_id or not tag:
        return {"error": "agent_id and tag are required"}

    suite = await auto_define_suite_from_disk(agent_id, ctx.workspace)
    if isinstance(suite, dict) and suite.get("error"):
        return suite

    suite_id = suite["id"]
    relative_path = f"docs/benchmarks/{agent_id}/suite.yaml"

    return cast(
        "dict[str, Any]",
        await _benchmark_run(
            {
                "agent_id": agent_id,
                "suite_id": suite_id,
                "tag": tag,
                "tasks": args.get("tasks"),
                "triggered_by": args.get("triggered_by"),
                "experiment_id": args.get("experiment_id"),
                "config_file": relative_path,
            },
            ctx,
        ),
    )
