"""Workflow tool-step guardrail enforcement.

Regression for the audit (2026-05-29): workflow tool steps called
``registry.execute`` directly, bypassing all guardrails — a workflow YAML could
run a destructive exec or a push to main unguarded.
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


def _engine_with_spy_registry():
    registry = SimpleNamespace(execute=AsyncMock(return_value={"ok": True}))
    runner = SimpleNamespace(registry=registry)
    config = SimpleNamespace(workspace="/tmp/ws")
    engine = WorkflowEngine.__new__(WorkflowEngine)
    engine.config = config
    engine.runner = runner
    engine._workflows = {}
    return engine, registry


def _step(tool_name: str, **args) -> WorkflowStepDef:
    return WorkflowStepDef(id="s1", type=WorkflowStepType.TOOL, tool_name=tool_name, tool_args=args)


def _result() -> WorkflowStepResult:
    return WorkflowStepResult(step_id="s1", step_type=WorkflowStepType.TOOL)


@pytest.mark.asyncio
async def test_destructive_exec_blocked_and_not_executed():
    engine, registry = _engine_with_spy_registry()
    run = WorkflowRun(workflow_id="wf1")
    result = _result()
    await engine._run_tool_step(_step("exec", command="rm -rf /tmp/data"), run, result)
    assert result.status == WorkflowStepStatus.FAILED
    assert "guardrail" in (result.error_message or "").lower()
    registry.execute.assert_not_awaited()  # blocked before reaching the tool


@pytest.mark.asyncio
async def test_push_to_main_blocked():
    engine, registry = _engine_with_spy_registry()
    run = WorkflowRun(workflow_id="wf1")
    result = _result()
    await engine._run_tool_step(_step("exec", command="git push origin main"), run, result)
    assert result.status == WorkflowStepStatus.FAILED
    registry.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_benign_tool_runs():
    engine, registry = _engine_with_spy_registry()
    run = WorkflowRun(workflow_id="wf1")
    result = _result()
    await engine._run_tool_step(_step("search_memory", query="x"), run, result)
    assert result.status == WorkflowStepStatus.COMPLETED
    registry.execute.assert_awaited_once()
