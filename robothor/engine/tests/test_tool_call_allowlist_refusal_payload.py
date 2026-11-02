"""The ``tool_call`` allow-list refusal carries the same gate and unlock as the direct one.

On a deferred run, ``tool_call`` refuses a wrapped tool outside the run's
deferred allow-set itself, before ``registry.execute``. Called directly, the
same tool outside the run's toolset is refused at admission with the v2
refusal payload (``tool_verdict``): gate ``allowlist``, gate name
``tools_allowed`` and an unlock naming the tool. The wrapped refusal used to
carry only ``error`` and ``did_you_mean``, so the model saw no gate and no way
out where the direct call would have told it both.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from robothor.engine.models import AgentConfig, TriggerType
from robothor.engine.tools.dispatch import ToolContext, clear_deferred_allowed, set_deferred_allowed
from robothor.engine.tools.handlers.toolsearch import _tool_call

WRAPPED = "claude_code_status"
GRANTED = frozenset({"read_file", "list_tasks"})


@pytest.fixture(autouse=True)
def _payload_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOTHOR_REFUSAL_PAYLOAD_V2", "1")


@pytest.fixture
def deferred_run() -> Any:
    token = set_deferred_allowed(GRANTED)
    yield
    clear_deferred_allowed(token)


async def _direct() -> Any:
    from robothor.engine.tool_admission import ToolAdmissionMixin

    run = SimpleNamespace(id="run-1", steps=[], trigger_type=TriggerType.TELEGRAM, tenant_id="t-1")
    return await ToolAdmissionMixin()._admit_tool_call(
        tc=SimpleNamespace(id="c1"),
        tool_name=WRAPPED,
        tool_args={"job_id": "x"},
        session=SimpleNamespace(run=run),
        agent_config=AgentConfig(id="example-agent", name="Example"),
        guardrail_engine=None,
        hook_registry=None,
        readonly_mode=False,
        readonly_tool_set=frozenset(),
        allowed_tool_set=frozenset({"tool_search", "tool_describe", "tool_call"}),
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("deferred_run")
async def test_the_wrapped_refusal_carries_the_direct_calls_gate_and_unlock() -> None:
    direct = await _direct()
    out = await _tool_call(
        {"name": WRAPPED, "arguments": {"job_id": "x"}}, ToolContext(agent_id="example-agent")
    )

    assert direct.allowed is False, "precondition: the direct call is refused"
    assert out.get("refused") is True, out
    assert (out.get("gate"), out.get("gate_name")) == (
        direct.output.get("gate"),
        direct.output.get("gate_name"),
    )
    assert f"'{WRAPPED}'" in out.get("unlock", ""), out
    assert "'tool_call'" not in out.get("unlock", "")


@pytest.mark.asyncio
@pytest.mark.usefixtures("deferred_run")
async def test_the_wrapped_refusal_keeps_its_error_and_suggestions() -> None:
    out = await _tool_call({"name": "list_task", "arguments": {}}, ToolContext(agent_id="a"))
    assert out["error"] == "tool 'list_task' is not in your allow-list"
    assert out["did_you_mean"] == ["list_tasks"] or "list_tasks" in out["did_you_mean"]
