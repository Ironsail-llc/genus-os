"""execute_code for an owner whose agent is container-sandboxed.

Today any container-sandboxed agent is refused execute_code: the tool proxy
lives in the engine and the container has no route to it. Under the playground
an owner or admin is not refused -- the snippet runs where the proxy is, on the
engine host, under their permissions. A member is refused as today.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.engine.code_exec_guards import preflight
from robothor.engine.sandbox import Sandbox, SandboxMode, sandbox_var


class _Run:
    def __init__(self, role: str) -> None:
        self.id = f"run-{role}"
        self.user_role = role
        self.parent_run_id = None


class _Session:
    def __init__(self, role: str) -> None:
        self.run = _Run(role)


class _Req:
    def __init__(self, role: str) -> None:
        self.session = _Session(role)


class _Proxy:
    def __init__(self, role: str) -> None:
        self.allowed = frozenset({"exec"})
        self._req = _Req(role)


@pytest.fixture
def boxed():
    token = sandbox_var.set(Sandbox(mode=SandboxMode.DOCKER, container_id="c1", run_id="r"))
    yield
    sandbox_var.reset(token)


def _refusal(role: str) -> dict[str, Any] | None:
    return preflight("print(1)", _Proxy(role))


def test_flag_off_refuses_the_owner_as_today(boxed, monkeypatch) -> None:
    monkeypatch.delenv("ROBOTHOR_SANDBOX_PLAYGROUND", raising=False)
    refusal = _refusal("owner")
    assert refusal is not None
    assert "container-sandboxed" in refusal["error"]


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_playground_lets_an_owner_run(boxed, monkeypatch, role) -> None:
    monkeypatch.setenv("ROBOTHOR_SANDBOX_PLAYGROUND", "1")
    assert _refusal(role) is None


def test_playground_still_refuses_a_member(boxed, monkeypatch) -> None:
    monkeypatch.setenv("ROBOTHOR_SANDBOX_PLAYGROUND", "1")
    refusal = _refusal("member")
    assert refusal is not None
    assert "container-sandboxed" in refusal["error"]
