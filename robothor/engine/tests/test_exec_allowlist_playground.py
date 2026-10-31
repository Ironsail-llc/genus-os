"""The exec allowlist under the sandbox playground.

An allowlist is the agent author's restriction on HOST commands. Inside a
container with no network and no host environment it guards nothing, and an
owner or admin is not bound by it: their command is allowed and recorded as an
observation. A member on the host is held to it as today. Flag off: unchanged.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from robothor.engine.guardrails import GuardrailEngine


def _engine() -> GuardrailEngine:
    return GuardrailEngine(
        enabled_policies=["exec_allowlist"],
        _exec_allowlists={"a": [re.compile(r"^git status$")]},
    )


def _check(*, user_role: str = "", sandboxed: bool = False, command: str = "curl example.com"):
    return _engine().check_pre_execution(
        "exec",
        {"command": command},
        agent_id="a",
        user_role=user_role,
        sandboxed=sandboxed,
    )


@pytest.fixture
def playground(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SANDBOX_PLAYGROUND", "1")


@pytest.fixture
def off(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_SANDBOX_PLAYGROUND", raising=False)


def test_flag_off_blocks_everyone_as_today(off) -> None:
    for role, sandboxed in (("owner", False), ("member", False), ("member", True)):
        result = _check(user_role=role, sandboxed=sandboxed)
        assert not result.allowed, (role, sandboxed)
        assert result.guardrail_name == "exec_allowlist"


def test_inside_a_container_the_allowlist_is_skipped(playground) -> None:
    result = _check(user_role="member", sandboxed=True)
    assert result.allowed
    assert result.action != "blocked"


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_an_owner_on_the_host_is_allowed_and_observed(playground, role) -> None:
    result = _check(user_role=role)
    assert result.allowed
    assert result.action == "observed"
    assert result.reason == f"exec_allowlist bypassed by role={role}"
    assert result.guardrail_name == "exec_allowlist"


def test_a_member_on_the_host_is_still_blocked(playground) -> None:
    result = _check(user_role="member")
    assert not result.allowed
    assert result.guardrail_name == "exec_allowlist"


def test_an_allowlisted_command_is_a_plain_allow_for_the_owner(playground) -> None:
    result = _check(user_role="owner", command="git status")
    assert result.allowed
    assert result.action != "observed"


class _Run:
    id = "run-1"
    steps: list[Any] = []
    tenant_id = "default"
    trigger_type = "manual"
    user_role = "owner"


class _Session:
    run = _Run()
    run_id = "run-1"


class _AgentConfig:
    id = "a"


class _Recorder:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def check_pre_execution(self, *_args: Any, **kwargs: Any) -> Any:
        self.kwargs = kwargs

        class _Ok:
            allowed = True
            action = "allowed"
            guardrail_name = ""
            reason = ""

        return _Ok()


async def _admit(monkeypatch) -> dict[str, Any]:
    from robothor.engine import tool_admission
    from robothor.engine.tool_admission import ToolAdmissionMixin

    class _Admitter(ToolAdmissionMixin):
        pass

    monkeypatch.setattr(tool_admission, "_log_guardrail_event", lambda **_k: None)
    recorder = _Recorder()
    verdict = await _Admitter()._check_guardrails(
        tc=None,
        tool_name="exec",
        tool_args={"command": "id"},
        session=_Session(),
        agent_config=_AgentConfig(),
        guardrail_engine=recorder,
    )
    assert verdict is None
    return recorder.kwargs


async def test_admission_passes_the_role_and_sandbox_state(playground, monkeypatch) -> None:
    kwargs = await _admit(monkeypatch)
    assert kwargs.get("user_role") == "owner"
    assert kwargs.get("sandboxed") is False


async def test_admission_flag_off_passes_nothing_new(off, monkeypatch) -> None:
    kwargs = await _admit(monkeypatch)
    assert "user_role" not in kwargs
    assert "sandboxed" not in kwargs
