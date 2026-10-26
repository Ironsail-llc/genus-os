"""A tool step whose command exits non-zero is a failed step, not a completed one.

Regression: the devops report's Telegram step crashed every week (Python
traceback, exit 1) while the workflow recorded it ``completed`` — ``exec``
reports a non-zero exit in ``exit_code``, not in an ``error`` key, and only
``error`` was checked. With ``on_failure: skip`` the step must now end
``skipped`` carrying the failure, and with ``abort`` it must stop the run.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robothor.engine.models import (
    WorkflowRun,
    WorkflowStepDef,
    WorkflowStepResult,
    WorkflowStepStatus,
    WorkflowStepType,
)
from robothor.engine.workflow import WorkflowEngine


def _engine(tool_result):
    registry = SimpleNamespace(execute=AsyncMock(return_value=tool_result))
    engine = WorkflowEngine.__new__(WorkflowEngine)
    engine.config = SimpleNamespace(workspace="/tmp/ws")
    engine.runner = SimpleNamespace(registry=registry)
    engine._workflows = {}
    return engine


def _step() -> WorkflowStepDef:
    return WorkflowStepDef(
        id="s1",
        type=WorkflowStepType.TOOL,
        tool_name="exec",
        tool_args={"command": "python scripts/x.py"},
        on_failure="skip",
    )


@pytest.mark.asyncio
async def test_nonzero_exit_code_fails_the_step():
    engine = _engine({"stdout": "", "stderr": "Traceback ... TypeError", "exit_code": 1})
    result = WorkflowStepResult(step_id="s1", step_type=WorkflowStepType.TOOL)
    await engine._run_tool_step(_step(), WorkflowRun(workflow_id="wf"), result)
    assert result.status == WorkflowStepStatus.FAILED
    assert "exit code 1" in result.error_message
    assert "TypeError" in result.error_message


@pytest.mark.asyncio
async def test_zero_exit_code_completes():
    engine = _engine({"stdout": "ok", "stderr": "", "exit_code": 0})
    result = WorkflowStepResult(step_id="s1", step_type=WorkflowStepType.TOOL)
    await engine._run_tool_step(_step(), WorkflowRun(workflow_id="wf"), result)
    assert result.status == WorkflowStepStatus.COMPLETED


@pytest.mark.asyncio
async def test_result_without_exit_code_completes():
    engine = _engine({"results": []})
    result = WorkflowStepResult(step_id="s1", step_type=WorkflowStepType.TOOL)
    await engine._run_tool_step(_step(), WorkflowRun(workflow_id="wf"), result)
    assert result.status == WorkflowStepStatus.COMPLETED
