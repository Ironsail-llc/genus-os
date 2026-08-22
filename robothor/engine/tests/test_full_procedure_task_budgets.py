"""The heaviest task in each suite needs a budget that reflects how it runs.

Measured 2026-08-22, after the per-task cap was raised 240s -> 900s: EXACTLY
ONE case in each of the four re-measured suites still timed out --

    curiosity-engine  basic-gap-analysis
    devops-analyst    incident-call-out
    agent-architect   fleet-analysis
    crm-hygiene       stale-task-cleanup

One per agent is not four independent bugs. Each is that suite's
full-procedure case: the one asking the agent to run its ENTIRE documented
workflow rather than a single step ("analyze knowledge gaps, pick the top 3,
research them, and store findings").

They are not hangs. agent-architect's PRODUCTION mean is 512s with a 728s max
and ZERO production timeouts, against `timeout_seconds: 0` (no wall-clock kill)
in `_defaults.yaml`. The benchmark was measuring its own cap, then recording
the result against the agent.

This pins the budget so the cap cannot silently drift back below what these
tasks demonstrably need.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

BENCH = Path(__file__).resolve().parents[3] / "docs" / "benchmarks"

# suite -> the case that times out at 900s, with its measured historical rate
FULL_PROCEDURE_CASES = {
    "curiosity-engine": ("basic-gap-analysis", "39% at the 240s cap"),
    "devops-analyst": ("incident-call-out", "40% at the 240s cap"),
    "agent-architect": ("fleet-analysis", "58% at the 240s cap"),
    "crm-hygiene": ("stale-task-cleanup", "timed out at 900s"),
}

MIN_BUDGET_SECONDS = 1800


def _task(suite: str, case_id: str) -> dict:
    data = yaml.safe_load((BENCH / suite / "suite.yaml").read_text()) or {}
    for task in data.get("tasks", []) or []:
        if task.get("id") == case_id:
            return task
    raise AssertionError(f"{suite}/{case_id} not found")


@pytest.mark.parametrize(
    ("suite", "case_id", "history"),
    [(s, c, h) for s, (c, h) in FULL_PROCEDURE_CASES.items()],
)
def test_full_procedure_case_has_a_realistic_budget(suite, case_id, history) -> None:
    task = _task(suite, case_id)
    budget = task.get("timeout_seconds")
    assert budget is not None, (
        f"{suite}/{case_id} runs the agent's whole procedure and historically "
        f"timed out ({history}). Without an explicit timeout_seconds it inherits "
        "the default cap and is graded on the harness's patience, not the agent."
    )
    assert budget >= MIN_BUDGET_SECONDS, (
        f"{suite}/{case_id} budget {budget}s is below the {MIN_BUDGET_SECONDS}s "
        "these tasks were measured to need"
    )


def test_the_default_cap_is_not_silently_lowered() -> None:
    """The default exists for ordinary cases; it must stay above production reality."""
    from robothor.engine.tools.handlers.benchmark import _DEFAULT_TASK_TIMEOUT_SECONDS

    assert _DEFAULT_TASK_TIMEOUT_SECONDS >= 900, (
        "agent-architect's production mean is 512s with a 728s max; a default "
        "below 900s reintroduces harness-timeout failures on ordinary tasks"
    )
