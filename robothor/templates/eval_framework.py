"""Agent test case generation and grading framework.

Generates test cases from manifests, grades agent runs against criteria,
and reviews transcripts for optimization opportunities.
"""

from __future__ import annotations

import contextlib
import json
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class TestCase:
    """A single test case for an agent."""

    id: str = ""
    prompt: str = ""
    expected_outputs: dict[str, Any] = field(default_factory=dict)
    agent_config_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass
class Criterion:
    """A grading criterion for an agent run."""

    name: str = ""
    check_fn: Callable[[dict[str, Any], list[dict[str, Any]]], bool] | None = None
    weight: float = 1.0


@dataclass
class EvalResult:
    """Result of grading an agent run."""

    test_case_id: str = ""
    run_id: str = ""
    passed_criteria: list[str] = field(default_factory=list)
    failed_criteria: list[str] = field(default_factory=list)
    tokens: int = 0
    cost: float = 0.0
    duration_ms: int = 0

    @property
    def score(self) -> float:
        total = len(self.passed_criteria) + len(self.failed_criteria)
        if total == 0:
            return 0.0
        return len(self.passed_criteria) / total


@dataclass
class TranscriptInsight:
    """A pattern detected in agent transcripts."""

    pattern: str = ""
    frequency: int = 0
    agent_ids: list[str] = field(default_factory=list)
    recommendation: str = ""


def generate_test_cases(manifest: dict[str, Any], instruction_content: str = "") -> list[TestCase]:
    """Auto-generate test cases from manifest analysis.

    Examines manifest fields to create appropriate test scenarios.
    """
    agent_id = manifest.get("id", "unknown")
    cases = []

    # Task protocol test
    if manifest.get("task_protocol"):
        cases.append(
            TestCase(
                id=f"{agent_id}-task-protocol",
                prompt="Process any pending tasks in your queue.",
                expected_outputs={
                    "calls_list_my_tasks": True,
                    "calls_resolve_task": True,
                },
                agent_config_overrides={"v2": {"cost_budget_usd": 0.50}},
            )
        )

    # Status file test
    status_file = manifest.get("status_file")
    if status_file:
        cases.append(
            TestCase(
                id=f"{agent_id}-status-file",
                prompt="Complete your routine check and update your status.",
                expected_outputs={
                    "writes_status_file": True,
                    "status_has_timestamp": True,
                },
                agent_config_overrides={"v2": {"cost_budget_usd": 0.50}},
            )
        )

    # Hook-triggered test
    hooks = manifest.get("hooks", [])
    for i, hook in enumerate(hooks):
        if isinstance(hook, dict):
            event_type = hook.get("event_type", "unknown")
            message = hook.get("message", f"Event {event_type} triggered.")
            cases.append(
                TestCase(
                    id=f"{agent_id}-hook-{i}",
                    prompt=message,
                    expected_outputs={"completes_without_error": True},
                    agent_config_overrides={"v2": {"cost_budget_usd": 0.50}},
                )
            )

    # Task creation test
    creates_for = manifest.get("creates_tasks_for", [])
    if creates_for:
        cases.append(
            TestCase(
                id=f"{agent_id}-creates-tasks",
                prompt="Process your inputs and route to downstream agents.",
                expected_outputs={
                    "calls_create_task": True,
                    "task_has_tags": True,
                },
                agent_config_overrides={"v2": {"cost_budget_usd": 0.50}},
            )
        )

    return cases


def grade_run(
    run: dict[str, Any],
    steps: list[dict[str, Any]],
    criteria: list[Criterion],
) -> EvalResult:
    """Grade an agent run against criteria.

    Args:
        run: The agent run dict (from tracking.get_run()).
        steps: The run steps (from tracking.list_steps()).
        criteria: List of criteria to check.
    """
    result = EvalResult(
        run_id=run.get("id", ""),
        tokens=(run.get("input_tokens") or 0) + (run.get("output_tokens") or 0),
        cost=run.get("total_cost_usd") or 0.0,
        duration_ms=run.get("duration_ms") or 0,
    )

    for criterion in criteria:
        if criterion.check_fn and criterion.check_fn(run, steps):
            result.passed_criteria.append(criterion.name)
        else:
            result.failed_criteria.append(criterion.name)

    return result


def _check_calls_tool(tool_name: str) -> Callable[[dict[str, Any], list[dict[str, Any]]], bool]:
    """Create a check function that verifies a tool was called."""

    def check(run: dict[str, Any], steps: list[dict[str, Any]]) -> bool:
        return any(
            s.get("tool_name") == tool_name
            for s in steps
            if s.get("step_type") in ("tool_call", "TOOL_CALL")
        )

    return check


def _check_no_errors(run: dict[str, Any], steps: list[dict[str, Any]]) -> bool:
    """Check that the run completed without errors."""
    return run.get("status") in ("completed", "COMPLETED") and not run.get("error_message")


def _check_writes_file(file_pattern: str) -> Callable[[dict[str, Any], list[dict[str, Any]]], bool]:
    """Create a check function that verifies a file was written."""

    def check(run: dict[str, Any], steps: list[dict[str, Any]]) -> bool:
        for step in steps:
            if step.get("tool_name") in ("write_file", "exec"):
                tool_input = step.get("tool_input")
                if isinstance(tool_input, str):
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        tool_input = json.loads(tool_input)
                if isinstance(tool_input, dict):
                    path = tool_input.get("path", "") or tool_input.get("command", "")
                    if file_pattern in str(path):
                        return True
        return False

    return check


def build_criteria_for_test_case(test_case: TestCase) -> list[Criterion]:
    """Build grading criteria from a test case's expected_outputs."""
    criteria = []

    expected = test_case.expected_outputs

    if expected.get("calls_list_my_tasks"):
        criteria.append(
            Criterion(name="calls_list_my_tasks", check_fn=_check_calls_tool("list_my_tasks"))
        )

    if expected.get("calls_resolve_task"):
        criteria.append(
            Criterion(name="calls_resolve_task", check_fn=_check_calls_tool("resolve_task"))
        )

    if expected.get("calls_create_task"):
        criteria.append(
            Criterion(name="calls_create_task", check_fn=_check_calls_tool("create_task"))
        )

    if expected.get("completes_without_error"):
        criteria.append(Criterion(name="completes_without_error", check_fn=_check_no_errors))

    if expected.get("writes_status_file"):
        criteria.append(Criterion(name="writes_status_file", check_fn=_check_writes_file("status")))

    return criteria


def review_transcripts(
    runs: list[dict[str, Any]],
    steps_by_run: dict[str, list[dict[str, Any]]],
) -> list[TranscriptInsight]:
    """Analyze recent runs for optimization opportunities.

    Args:
        runs: List of agent run dicts.
        steps_by_run: Mapping of run_id -> list of step dicts.
    """
    insights = []

    # Detect repeated identical tool calls within a single run
    repeated_runs = []
    for run in runs:
        run_id = run.get("id", "")
        steps = steps_by_run.get(run_id, [])
        seen_calls: dict[str, int] = {}
        for step in steps:
            if step.get("step_type") in ("tool_call", "TOOL_CALL"):
                key = f"{step.get('tool_name')}:{json.dumps(step.get('tool_input'), sort_keys=True, default=str)}"
                seen_calls[key] = seen_calls.get(key, 0) + 1
        if any(count > 1 for count in seen_calls.values()):
            repeated_runs.append(run.get("agent_id", run_id))

    if repeated_runs:
        insights.append(
            TranscriptInsight(
                pattern="Repeated identical tool calls in single run",
                frequency=len(repeated_runs),
                agent_ids=list(set(repeated_runs)),
                recommendation="Investigate why the agent calls the same tool with identical args multiple times",
            )
        )

    # Detect error-retry loops
    error_retry_runs = []
    for run in runs:
        run_id = run.get("id", "")
        steps = steps_by_run.get(run_id, [])
        prev_error_tool = None
        for step in steps:
            if step.get("error_message"):
                prev_error_tool = step.get("tool_name")
            elif prev_error_tool and step.get("tool_name") == prev_error_tool:
                error_retry_runs.append(run.get("agent_id", run_id))
                break
            else:
                prev_error_tool = None

    if error_retry_runs:
        insights.append(
            TranscriptInsight(
                pattern="Error-retry loops (same tool retried after error)",
                frequency=len(error_retry_runs),
                agent_ids=list(set(error_retry_runs)),
                recommendation="Add error handling guidance in instruction file to prevent blind retries",
            )
        )

    # Detect runs hitting max_iterations
    max_iter_runs = [
        r.get("agent_id", r.get("id", ""))
        for r in runs
        if r.get("budget_exhausted") or r.get("status") in ("budget_exhausted", "BUDGET_EXHAUSTED")
    ]
    if max_iter_runs:
        insights.append(
            TranscriptInsight(
                pattern="Runs hitting budget/iteration limits",
                frequency=len(max_iter_runs),
                agent_ids=list(set(max_iter_runs)),
                recommendation="Consider increasing max_iterations or simplifying the task scope",
            )
        )

    # Cost outliers (>2 sigma from mean)
    costs = [r.get("total_cost_usd") or 0.0 for r in runs if r.get("total_cost_usd")]
    if len(costs) >= 3:
        mean_cost = statistics.mean(costs)
        stdev_cost = statistics.stdev(costs)
        if stdev_cost > 0:
            outlier_runs = [
                r.get("agent_id", r.get("id", ""))
                for r in runs
                if (r.get("total_cost_usd") or 0.0) > mean_cost + 2 * stdev_cost
            ]
            if outlier_runs:
                insights.append(
                    TranscriptInsight(
                        pattern=f"Cost outliers (>{mean_cost + 2 * stdev_cost:.4f} USD, mean={mean_cost:.4f})",
                        frequency=len(outlier_runs),
                        agent_ids=list(set(outlier_runs)),
                        recommendation="Review these runs for unnecessary tool calls or model upgrades",
                    )
                )

    return insights
