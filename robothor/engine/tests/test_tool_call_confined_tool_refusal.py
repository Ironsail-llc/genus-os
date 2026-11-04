"""A confined tool sent through ``tool_call`` is refused like the direct call.

A run whose execution plan is refused (no
container runtime, and nobody with an owner or admin role driving the run) has
the tools a container confines -- ``browser`` and ``desktop_*`` -- refused per
call by ``GuardrailEngine.check_pre_execution``, or they would reach the engine
host. With deferred tool loading on by default those tools are usually reached
as ``tool_call(name=..., arguments=...)``, and admission judges the wrapped
call (``wrapped_call.inner_call``). This pins that the refusal reaches the
wrapped confined tool exactly as it reaches the direct one, for the same
member on the same refused plan, and that the verdict stays the wrapper's own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from robothor.engine import sandbox_policy as sp
from robothor.engine import tool_admission
from robothor.engine.guardrails import GuardrailEngine
from robothor.engine.models import TriggerType
from robothor.engine.sandbox import RuntimeStatus
from robothor.engine.tool_admission import ToolAdmissionMixin

AGENT = "example-agent"
ABSENT = RuntimeStatus(binary="podman", available=False, reason="neither podman nor docker")


@dataclass
class _Cfg:
    id: str = AGENT
    sandbox: str = "docker"
    tools_allowed: list[str] = field(default_factory=lambda: ["exec", "browser", "desktop_click"])
    tools_denied: list[str] = field(default_factory=list)
    guardrails: list[str] = field(default_factory=list)
    guardrails_opt_out: bool = False
    service_role = "service"
    human_approval_timeout = 1.0
    human_approval_fail_open = False


class _Run:
    def __init__(self, run_id: str, role: str) -> None:
        self.id = run_id
        self.user_role = role
        self.parent_run_id = None
        self.trigger_type = TriggerType.MANUAL
        self.nesting_depth = 0
        self.is_benchmark = False
        self.steps: list[Any] = []
        self.tenant_id = "default"


class _Session:
    def __init__(self, run: _Run) -> None:
        self.run = run
        self.run_id = run.id


class _Admitter(ToolAdmissionMixin):
    """The mixin's gates, with nothing else of the runner attached."""


@pytest.fixture(autouse=True)
def _playground(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("robothor.engine.sandbox.runtime_status", lambda **_k: ABSENT)
    monkeypatch.setattr(sp, "_record", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_admission, "_log_guardrail_event", lambda **_k: None)
    monkeypatch.setattr("robothor.engine.tracking.log_tool_event", lambda **_k: None)


async def _member_session(run_id: str) -> _Session:
    run = _Run(run_id, "member")
    plan = await sp.establish_execution_plan(_Cfg(), "off", agent_id=AGENT, run=run)
    assert plan.where == "refuse", "precondition: a member with no runtime is refused"
    return _Session(run)


async def _admit(session: _Session, tool_name: str, tool_args: dict[str, Any]) -> Any:
    return await _Admitter()._admit_tool_call(
        tc=MagicMock(id="call_1"),
        tool_name=tool_name,
        tool_args=tool_args,
        session=session,
        agent_config=_Cfg(),  # type: ignore[arg-type]
        guardrail_engine=GuardrailEngine(enabled_policies=["no_destructive_writes"]),
        hook_registry=None,
        readonly_mode=False,
        readonly_tool_set=frozenset(),
        allowed_tool_set=frozenset(),
    )


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("browser", {"action": "open", "url": "https://example.com"}), ("desktop_click", {})],
)
async def test_a_confined_tool_through_tool_call_is_refused_like_the_direct_call(
    tool: str, arguments: dict[str, Any]
) -> None:
    session = await _member_session(f"run-member-{tool}")

    direct = await _admit(session, tool, dict(arguments))
    wrapper = {"name": tool, "arguments": dict(arguments)}
    wrapped = await _admit(session, "tool_call", wrapper)

    assert direct.allowed is False, "precondition: the direct call is refused"
    assert wrapped.allowed is False, f"tool_call(name={tool!r}) reached a confined tool"
    assert (wrapped.message, wrapped.output) == (direct.message, direct.output)
    assert wrapped.tool_args == wrapper, "the refusal is the wrapper's own"


async def test_a_wrapped_tool_the_container_does_not_confine_is_not_refused_by_it() -> None:
    session = await _member_session("run-member-read")
    verdict = await _admit(
        session, "tool_call", {"name": "read_file", "arguments": {"path": "notes.md"}}
    )
    assert verdict.allowed is True
