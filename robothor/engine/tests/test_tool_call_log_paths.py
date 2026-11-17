"""The two call paths that bypass the turn's refusal recorder also log.

``execute_code``'s tool proxy runs every admission gate itself and returns a
refusal before the registry, and refuses on its own two caps. A workflow tool
step reaches the registry with no ``run_id``. Each still writes one
``event=tool_call`` line, the workflow's naming its run.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import (
    AgentConfig,
    DeliveryMode,
    WorkflowRun,
    WorkflowStepDef,
    WorkflowStepResult,
    WorkflowStepType,
)
from robothor.engine.session import AgentSession
from robothor.engine.tests.test_tool_call_log import LINE_LOGGER, _fields
from robothor.engine.tool_proxy import RunToolProxy, proxy_allow_set
from robothor.engine.tool_turn import ToolTurnRequest
from robothor.engine.tools.registry import ToolRegistry


def _lines(caplog) -> list[dict[str, str]]:
    return [
        _fields(r.getMessage())
        for r in caplog.records
        if r.name == LINE_LOGGER and "event=tool_call" in r.getMessage()
    ]


@pytest.fixture
def runner(engine_config):
    from robothor.engine.runner import AgentRunner

    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.execute = AsyncMock(return_value={"ok": True})
        registry.registered_tool_names.return_value = {"exec", "read_file", "write_file"}
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


def _proxy(runner, *, readonly=False, max_calls=10):
    config = AgentConfig(
        id="example-agent",
        name="Example",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        delivery_mode=DeliveryMode.NONE,
    )
    session = AgentSession(agent_id=config.id)
    session.run.agent_id = config.id
    req = ToolTurnRequest(
        assistant_msg=MagicMock(tool_calls=[]),
        session=session,
        agent_config=config,
        iteration=0,
        allowed_tool_set=frozenset({"exec", "read_file", "write_file"}),
        readonly_mode=readonly,
        readonly_tool_set=frozenset({"read_file"}),
    )
    return RunToolProxy(
        runner=runner,
        req=req,
        allowed=proxy_allow_set(req, runner.registry),
        max_calls=max_calls,
        max_approvals=1,
    ), session


@pytest.mark.asyncio
async def test_a_proxied_call_refused_at_admission_writes_one_line(runner, caplog) -> None:
    caplog.set_level(logging.INFO)
    proxy, session = _proxy(runner, readonly=True)
    result = await proxy.call("write_file", {"path": "a.txt", "content": "x"})
    assert "plan mode" in result["error"]
    (line,) = _lines(caplog)
    assert line["tool"] == "write_file"
    assert line["agent"] == "example-agent"
    assert line["run_id"] == session.run.id
    assert line["outcome"] in ("refused", "denied")


@pytest.mark.asyncio
async def test_a_proxied_call_over_the_cap_writes_one_line(runner, caplog) -> None:
    caplog.set_level(logging.INFO)
    proxy, _session = _proxy(runner, max_calls=0)
    result = await proxy.call("read_file", {"path": "a.txt"})
    assert result["guard"] == "execute_code_call_cap"
    (line,) = _lines(caplog)
    assert line["outcome"] == "refused"
    assert line["error_type"] == "policy.execute_code_call_cap"


@pytest.mark.asyncio
async def test_a_workflow_tool_step_line_names_its_run(monkeypatch, caplog) -> None:
    from robothor.engine.workflow import WorkflowEngine

    async def _execute_tool(name: str, args: dict[str, Any], **kwargs: Any) -> Any:
        return {"ok": True}

    monkeypatch.setattr("robothor.engine.tools.registry._execute_tool", _execute_tool)
    engine = WorkflowEngine.__new__(WorkflowEngine)
    engine.config = SimpleNamespace(workspace="/tmp/ws")
    engine.runner = SimpleNamespace(registry=ToolRegistry.__new__(ToolRegistry))
    engine._workflows = {}
    run = WorkflowRun(id="wr-1", workflow_id="example-flow")
    step = WorkflowStepDef(
        id="s1", type=WorkflowStepType.TOOL, tool_name="search_memory", tool_args={"query": "x"}
    )
    caplog.set_level(logging.INFO)
    await engine._run_tool_step(
        step, run, WorkflowStepResult(step_id="s1", step_type=WorkflowStepType.TOOL)
    )
    (line,) = _lines(caplog)
    assert line["workflow"] == "example-flow"
    assert line["run_id"] == "wr-1"
