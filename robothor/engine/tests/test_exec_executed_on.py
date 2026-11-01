"""Every `exec` says where it ran, and a refused one says how to unlock it.

With ROBOTHOR_SANDBOX_PLAYGROUND on, the result of `exec` carries
`executed_on` -- `sandbox`, `host` or `host_service` -- and, when a container
was wanted but the command ran on the host, an `execution_note` the agent can
repeat to the owner. A run whose plan is `refuse` never starts a subprocess:
the result names the gate and the unlock with `error_type: exec_unavailable`.
With the flag off the result is exactly what it is today.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from robothor.engine import sandbox_policy as sp
from robothor.engine.sandbox import RuntimeStatus, Sandbox, SandboxMode, sandbox_var

ABSENT = RuntimeStatus(binary="podman", available=False, reason="neither podman nor docker")


class _Cfg:
    id = "example-agent"
    sandbox = "docker"
    tools_allowed = ["exec"]
    tools_denied: list[str] = []


class _Ctx:
    def __init__(self, run_id: str) -> None:
        self.workspace = str(Path.cwd())
        self.agent_id = "example-agent"
        self.run_id = run_id
        self.tenant_id = "default"


def _exec():
    from robothor.engine.tools.handlers.filesystem import HANDLERS

    return HANDLERS["exec"]


@pytest.fixture
def events(monkeypatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "robothor.engine.tracking.log_guardrail_event",
        lambda *a, **k: recorded.append({"args": a, **k}),
    )
    return recorded


@pytest.fixture
def playground(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SANDBOX_PLAYGROUND", "1")


def _plan_for(run_id: str, role: str) -> None:
    sp.set_exec_plan(
        run_id,
        sp.resolve_execution_plan(
            _Cfg(), "off", agent_id="example-agent", user_role=role, runtime=ABSENT
        ),
    )


async def test_flag_off_the_result_is_unchanged(monkeypatch, events) -> None:
    monkeypatch.delenv("ROBOTHOR_SANDBOX_PLAYGROUND", raising=False)
    result = await _exec()({"command": "echo hi"}, _Ctx("run-off"))
    assert result["exit_code"] == 0
    assert "executed_on" not in result
    assert "execution_note" not in result
    assert [e for e in events if e.get("guardrail_name") == "exec_location"] == []


async def test_a_plain_host_exec_says_host(playground, events) -> None:
    result = await _exec()({"command": "echo hi"}, _Ctx("run-plain"))
    assert result["exit_code"] == 0
    assert result["executed_on"] == "host"
    assert "execution_note" not in result
    located = [e for e in events if e.get("guardrail_name") == "exec_location"]
    assert len(located) == 1
    assert located[0]["action"] == "allowed"
    assert "host" in (located[0].get("reason") or "")


async def test_a_container_exec_says_sandbox(playground, events) -> None:
    class _Recording(Sandbox):
        async def exec_shell(self, command: str, timeout: int = 30) -> dict[str, Any]:
            return {"stdout": "in-container", "stderr": "", "exit_code": 0}

    token = sandbox_var.set(_Recording(mode=SandboxMode.DOCKER, container_id="c1", run_id="r"))
    try:
        result = await _exec()({"command": "whoami"}, _Ctx("run-box"))
    finally:
        sandbox_var.reset(token)
    assert result["executed_on"] == "sandbox"
    assert len([e for e in events if e.get("guardrail_name") == "exec_location"]) == 1


async def test_an_owner_without_a_runtime_runs_on_the_host_with_a_note(playground, events) -> None:
    _plan_for("run-owner", "owner")
    result = await _exec()({"command": "echo owner"}, _Ctx("run-owner"))
    assert result["exit_code"] == 0
    assert "owner" in result["stdout"]
    assert result["executed_on"] == "host"
    note = result["execution_note"]
    assert "engine host" in note
    assert "no container runtime" in note
    assert "owner" in note


async def test_a_member_without_a_runtime_is_refused_and_nothing_runs(
    playground, events, monkeypatch
) -> None:
    import subprocess

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a refused exec must not start a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    _plan_for("run-member", "member")
    result = await _exec()({"command": "echo member"}, _Ctx("run-member"))
    assert result["error_type"] == "exec_unavailable"
    assert "no sandbox runtime" in result["error"]
    assert "member" in result["error"]
    assert "owner or admin" in result["unlock"]
    assert "executed_on" not in result


async def test_the_host_service_branch_says_host_service(playground, events, monkeypatch) -> None:
    from robothor.engine import host_execution

    async def _execute(_ctx: Any, _command: str, _timeout: int, _env: Any) -> dict[str, Any]:
        return {"stdout": "via service", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(host_execution, "socket_path", lambda: "/run/example.sock")
    monkeypatch.setattr(host_execution, "eligible", lambda _ctx: True)
    monkeypatch.setattr(host_execution, "execute", _execute)
    result = await _exec()({"command": "id"}, _Ctx("run-svc"))
    assert result["executed_on"] == "host_service"
