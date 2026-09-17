"""A denied approval must tell the agent WHY it will stay denied.

2026-09-17. When a human-approval gate denies a call — the operator tapped
Deny, or nobody answered and ``human_approval_timeout`` expired — the agent was
told "Denied by operator (human_approval): <reason>". From inside the turn that
reads like a transient refusal, so an autonomous run retries, is denied again,
and burns its budget asking a person who is not there.

The gate is INSTANCE configuration: a key in this agent's own manifest that the
agent cannot see. The denial now names it, so the run can route around the
gated tool instead of re-queuing behind it. Genus OS runs autonomously by
default; when a run cannot do something, the remedy is another route, not
another prompt.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.engine import tool_admission
from robothor.engine.tool_admission import ToolAdmissionMixin


class _Run:
    id = "run-1"
    steps: list[Any] = []
    tenant_id = "default"
    trigger_type = "manual"


class _Session:
    run = _Run()
    run_id = "run-1"


class _AgentConfig:
    id = "steward"
    human_approval_timeout = 1.0
    human_approval_fail_open = False


class _Guardrail:
    allowed = False
    action = "escalate"
    guardrail_name = "human_approval"
    reason = "delete_person requires explicit human approval"


class _GuardrailEngine:
    @staticmethod
    def check_pre_execution(*_args: Any, **_kwargs: Any) -> _Guardrail:
        return _Guardrail()


class _Admitter(ToolAdmissionMixin):
    """The mixin's guardrail gate, with nothing else of the runner attached."""


async def _denial(monkeypatch: pytest.MonkeyPatch, *, approver: bool) -> str:
    if approver:

        class _Manager:
            @staticmethod
            async def request_approval(**_kwargs: Any) -> bool:
                return False  # the operator denied, or the timeout fired

        monkeypatch.setattr(
            "robothor.engine.permission_escalation.get_permission_manager",
            lambda: _Manager(),
        )
    else:
        monkeypatch.setattr(
            "robothor.engine.permission_escalation.get_permission_manager",
            lambda: None,
        )
        monkeypatch.setattr(
            "robothor.engine.permission_escalation.fail_closed_on_missing_manager",
            lambda: True,
        )
        monkeypatch.setattr(tool_admission, "_log_guardrail_event", lambda **_k: None)
        monkeypatch.setattr("robothor.engine.feature_flags.approval_mode", lambda: "enforce")

    verdict = await _Admitter()._check_guardrails(
        tc=None,
        tool_name="delete_person",
        tool_args={"person_id": "p-1"},
        session=_Session(),
        agent_config=_AgentConfig(),
        guardrail_engine=_GuardrailEngine(),
    )
    assert verdict is not None and verdict.allowed is False
    return str(verdict.message)


@pytest.mark.asyncio
async def test_an_operator_denial_names_the_manifest_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = await _denial(monkeypatch, approver=True)

    assert "human_approval_tools" in message, message
    assert "instance configuration" in message, message
    assert "retry" in message.lower(), message


@pytest.mark.asyncio
async def test_a_timeout_with_no_approver_names_it_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fail-closed branch is the one an unattended schedule actually hits:
    nobody is watching, so nobody answers."""
    message = await _denial(monkeypatch, approver=False)

    assert "human_approval_tools" in message, message
    assert "instance configuration" in message, message


@pytest.mark.asyncio
async def test_the_original_reason_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new sentence is added to the denial, not instead of it."""
    message = await _denial(monkeypatch, approver=True)

    assert "requires explicit human approval" in message, message
