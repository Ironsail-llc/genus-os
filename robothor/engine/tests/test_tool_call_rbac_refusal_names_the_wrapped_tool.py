"""A system-run RBAC refusal through ``tool_call`` names the tool it wraps.

Admission judges a ``tool_call`` as the call it wraps (``wrapped_call``), so a
scheduled run whose service role may not use ``web_render`` is refused it
whether it calls the tool directly or through ``tool_call``. The refusal's
gate and unlock (``tool_verdict.with_gate``) must say the same thing in both
cases: the role may not use ``'web_render'``. Naming ``'tool_call'`` tells the
model, and the operator reading the step, to grant the wrong tool.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.models import AgentConfig, TriggerType

DENIED = "web_render"
ARGS = {"url": "https://example.com"}


@pytest.fixture(autouse=True)
def _payload_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOTHOR_REFUSAL_PAYLOAD_V2", "1")


def _classify(_role: str, _tenant: str, tool_name: str, _mode: str) -> tuple[str, str]:
    if tool_name == DENIED:
        return ("block", f"Role 'service' denied '{tool_name}'")
    return ("allow", "")


async def _admit(tool_name: str, tool_args: dict[str, Any]) -> Any:
    from robothor.engine.tool_admission import ToolAdmissionMixin

    run = SimpleNamespace(id="run-1", steps=[], trigger_type=TriggerType.CRON, tenant_id="t-1")
    with (
        patch("robothor.engine.feature_flags.rbac_enforcement_mode", return_value="enforce"),
        patch("robothor.engine.permissions.classify_system_tool_access", side_effect=_classify),
        patch("robothor.engine.tracking.log_guardrail_event"),
    ):
        return await ToolAdmissionMixin()._admit_tool_call(
            tc=SimpleNamespace(id="c1"),
            tool_name=tool_name,
            tool_args=tool_args,
            session=SimpleNamespace(run=run),
            agent_config=AgentConfig(id="example-agent", name="Example"),
            guardrail_engine=None,
            hook_registry=None,
            readonly_mode=False,
            readonly_tool_set=frozenset(),
            allowed_tool_set=frozenset(),
        )


async def test_the_direct_refusal_names_the_tool() -> None:
    verdict = await _admit(DENIED, dict(ARGS))
    assert verdict.allowed is False
    assert verdict.gate == "role"
    assert f"'{DENIED}'" in verdict.unlock


async def test_a_wrapped_refusal_names_the_wrapped_tool_not_the_wrapper() -> None:
    wrapper = {"name": DENIED, "arguments": dict(ARGS)}
    verdict = await _admit("tool_call", wrapper)

    assert verdict.allowed is False
    assert f"'{DENIED}'" in verdict.unlock
    assert "'tool_call'" not in verdict.unlock
    assert verdict.output.get("unlock") == verdict.unlock
    assert verdict.tool_args == wrapper, "the refusal is still the wrapper's own"


async def test_the_wrapped_refusal_reads_as_the_direct_one() -> None:
    direct = await _admit(DENIED, dict(ARGS))
    wrapped = await _admit("tool_call", {"name": DENIED, "arguments": dict(ARGS)})
    assert (wrapped.gate, wrapped.unlock, wrapped.message) == (
        direct.gate,
        direct.unlock,
        direct.message,
    )
