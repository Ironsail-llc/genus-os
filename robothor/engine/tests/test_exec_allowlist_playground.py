"""The exec allowlist under the sandbox playground.

An allowlist is the agent author's restriction on commands. An owner or admin
DRIVING this run (a person on an interactive surface, on the run nothing
spawned) is not bound by it: their command is allowed and recorded as an
observation. Everyone else keeps it, inside a container or out: a member, a
cron or other automated run whose service role happens to read "owner", and a
sub-agent an owner's chat spawned.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from robothor.engine.guardrails import GuardrailEngine
from robothor.engine.sandbox import Sandbox, SandboxMode, sandbox_var
from robothor.engine.sandbox_policy import guardrail_context


def _session(*, role: str, trigger: str, depth: int = 0, user_id: str = "u-1") -> Any:
    run = type(
        "_R",
        (),
        {
            "id": f"run-{trigger}-{depth}",
            "user_role": role,
            "user_id": user_id,
            "trigger_type": trigger,
            "nesting_depth": depth,
            "parent_run_id": None,
        },
    )()
    return type("_S", (), {"run": run})()


def _engine() -> GuardrailEngine:
    return GuardrailEngine(
        enabled_policies=["exec_allowlist"],
        _exec_allowlists={"a": [re.compile(r"^git status$")]},
    )


def _check(*, user_role: str = "", command: str = "curl example.com"):
    return _engine().check_pre_execution(
        "exec",
        {"command": command},
        agent_id="a",
        user_role=user_role,
    )


def test_a_member_inside_a_container_keeps_the_allowlist() -> None:
    """A container may have bridge networking and always has the workspace
    mounted, so the allowlist still guards something there."""
    token = sandbox_var.set(Sandbox(mode=SandboxMode.DOCKER, container_id="c1", run_id="r"))
    try:
        kwargs = guardrail_context(_session(role="member", trigger="webchat"))
    finally:
        sandbox_var.reset(token)
    result = _engine().check_pre_execution(
        "exec", {"command": "curl example.com"}, agent_id="a", **kwargs
    )
    assert not result.allowed
    assert result.guardrail_name == "exec_allowlist"


@pytest.mark.parametrize(
    ("trigger", "depth", "user_id"),
    [
        ("cron", 0, "service:a"),  # a service role that reads "owner"
        ("hook", 0, ""),
        ("workflow", 0, "service:workflow:w"),
        ("sub_agent", 1, "u-owner"),  # spawned from an owner's chat
        ("webchat", 0, "service:a"),  # a service credential on webchat
    ],
)
def test_only_a_run_an_owner_is_driving_gets_the_bypass(trigger, depth, user_id) -> None:
    kwargs = guardrail_context(
        _session(role="owner", trigger=trigger, depth=depth, user_id=user_id)
    )
    result = _engine().check_pre_execution(
        "exec", {"command": "curl example.com"}, agent_id="a", **kwargs
    )
    assert not result.allowed, (trigger, depth, user_id)
    assert result.guardrail_name == "exec_allowlist"


@pytest.mark.parametrize("trigger", ["webchat", "telegram", "slack", "ide", "manual"])
def test_an_owner_driving_an_interactive_run_gets_the_bypass(trigger) -> None:
    kwargs = guardrail_context(_session(role="owner", trigger=trigger))
    result = _engine().check_pre_execution(
        "exec", {"command": "curl example.com"}, agent_id="a", **kwargs
    )
    assert result.allowed
    assert result.action == "observed"


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_an_owner_on_the_host_is_allowed_and_observed(role) -> None:
    result = _check(user_role=role)
    assert result.allowed
    assert result.action == "observed"
    assert result.reason == f"exec_allowlist bypassed by role={role}"
    assert result.guardrail_name == "exec_allowlist"


def test_a_member_on_the_host_is_still_blocked() -> None:
    result = _check(user_role="member")
    assert not result.allowed
    assert result.guardrail_name == "exec_allowlist"


def test_an_allowlisted_command_is_a_plain_allow_for_the_owner() -> None:
    result = _check(user_role="owner", command="git status")
    assert result.allowed
    assert result.action != "observed"


class _Run:
    id = "run-1"
    steps: list[Any] = []
    tenant_id = "default"
    trigger_type = "manual"
    user_role = "owner"
    user_id = "u-owner"


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


async def test_admission_passes_the_acting_role(monkeypatch) -> None:
    kwargs = await _admit(monkeypatch)
    assert kwargs.get("user_role") == "owner"
    assert "sandboxed" not in kwargs
