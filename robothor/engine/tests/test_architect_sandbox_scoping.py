"""A case that asserts a write must be scoped to somewhere it can write.

agent-architect's `dispatch-routing` and `cross-pollination` assert
`tools_used: [create_task]`, but `create_task` is in
`benchmark_sandbox.SANDBOX_WRITE_TOOLS` -- granted only to a task that is
scoped to the isolated benchmark-sandbox tenant. Neither case declared
fixtures or state_checks, so neither was ever scoped, so `create_task` was
denied and both failed on the harness rather than on the agent.

Measured 2026-08-22: agent-architect went 0.417 -> 0.429 after the grader
repairs, while devops-analyst went 0.542 -> 0.750 on the same fixes. All four
of its remaining failures were infrastructure, two of them this one.

Declaring `state_checks` scopes the task (an empty sandbox is the abstention
case) AND replaces "did it name create_task" with "did a task row appear",
which is the stronger assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

SUITE = (
    Path(__file__).resolve().parents[3] / "docs" / "benchmarks" / "agent-architect" / "suite.yaml"
)
WRITE_CASES = ("dispatch-routing", "cross-pollination")


def _tasks() -> dict[str, dict]:
    data = yaml.safe_load(SUITE.read_text()) or {}
    return {t["id"]: t for t in data.get("tasks", [])}


@pytest.mark.parametrize("case_id", WRITE_CASES)
def test_a_case_asserting_a_write_tool_is_sandbox_scoped(case_id: str) -> None:
    """Asserting a sandbox-write tool without being scoped is unpassable."""
    from robothor.engine.benchmark_sandbox import SANDBOX_WRITE_TOOLS

    task = _tasks()[case_id]
    expected = task.get("expected", {}) or {}
    asserted = set(expected.get("tools_used") or [])
    needs_sandbox = asserted & set(SANDBOX_WRITE_TOOLS)
    if not needs_sandbox:
        pytest.skip(f"{case_id} asserts no sandbox-write tool")

    scoped = bool(task.get("fixtures")) or bool(expected.get("state_checks"))
    assert scoped, (
        f"{case_id} asserts {sorted(needs_sandbox)} but declares neither fixtures "
        "nor state_checks, so it is never scoped to the sandbox tenant and the "
        "tool is denied -- the case fails on the harness, not on the agent"
    )


def test_every_suite_case_asserting_a_write_is_scoped() -> None:
    """The general rule, across every suite -- this class of defect is easy to
    reintroduce by adding a tools_used entry without a scoping declaration."""
    from robothor.engine.benchmark_sandbox import SANDBOX_WRITE_TOOLS

    root = SUITE.parents[1]
    offenders: list[str] = []
    for suite_path in sorted(root.glob("*/suite.yaml")):
        data = yaml.safe_load(suite_path.read_text()) or {}
        for task in data.get("tasks", []) or []:
            expected = task.get("expected", {}) or {}
            needs = set(expected.get("tools_used") or []) & set(SANDBOX_WRITE_TOOLS)
            if not needs:
                continue
            if not (task.get("fixtures") or expected.get("state_checks")):
                offenders.append(f"{suite_path.parent.name}/{task.get('id')} needs {sorted(needs)}")
    assert not offenders, (
        "these cases assert a sandbox-write tool but are never scoped to the "
        "sandbox, so the tool is denied and they cannot pass:\n  " + "\n  ".join(offenders)
    )
