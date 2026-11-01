"""execute_code for an owner whose agent is container-sandboxed.

Today any container-sandboxed agent is refused execute_code: the tool proxy
lives in the engine and the container has no route to it. Under the playground
an owner or admin DRIVING the run is not refused -- the snippet runs where the
proxy is, on the engine host, under their permissions, and that is recorded:
the result says `executed_on: host` and an `exec_location` row names the live
container it ran beside. A member, an automated run (whatever its service role
reads) and a sub-agent are refused as today.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.engine.code_exec_guards import preflight
from robothor.engine.sandbox import Sandbox, SandboxMode, sandbox_var


class _Run:
    def __init__(self, role: str, trigger: str = "webchat", user_id: str = "u-1") -> None:
        self.id = f"run-{role}-{trigger}"
        self.user_role = role
        self.user_id = user_id
        self.trigger_type = trigger
        self.nesting_depth = 0
        self.parent_run_id = None


class _Session:
    def __init__(self, role: str, **kw: Any) -> None:
        self.run = _Run(role, **kw)


class _Req:
    def __init__(self, role: str, **kw: Any) -> None:
        self.session = _Session(role, **kw)


class _Proxy:
    def __init__(self, role: str, **kw: Any) -> None:
        self.allowed = frozenset({"exec"})
        self._req = _Req(role, **kw)
        self.responses: list[Any] = []


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


@pytest.mark.parametrize(
    ("trigger", "user_id"),
    [("cron", "service:example-agent"), ("event", ""), ("webchat", "service:example-agent")],
)
def test_playground_refuses_an_owner_role_no_person_is_driving(
    boxed, monkeypatch, trigger, user_id
) -> None:
    monkeypatch.setenv("ROBOTHOR_SANDBOX_PLAYGROUND", "1")
    refusal = preflight("print(1)", _Proxy("owner", trigger=trigger, user_id=user_id))
    assert refusal is not None
    assert "container-sandboxed" in refusal["error"]


async def test_an_owners_snippet_beside_a_live_container_is_recorded(boxed, monkeypatch) -> None:
    """The widening is explicit: the result and the audit trail both say it ran
    on the host, and that a container was live when it did."""
    from robothor.engine import code_exec_rpc, tool_proxy
    from robothor.engine import sandbox_policy as sp
    from robothor.engine.tools.handlers import code_exec

    monkeypatch.setenv("ROBOTHOR_SANDBOX_PLAYGROUND", "1")
    proxy = _Proxy("owner")
    run_id = proxy._req.session.run.id
    sp.set_exec_plan(run_id, sp.ExecPlan(where="sandbox", user_role="owner"))
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "robothor.engine.tracking.log_guardrail_event", lambda *a, **k: events.append(k)
    )

    class _Server:
        def __init__(self, **_k: Any) -> None:
            pass

        async def start(self) -> None:
            pass

        async def aclose(self) -> None:
            pass

        def bind_to_session(self, *_a: Any) -> None:
            pass

    class _Result:
        stdout = "1\n"
        timed_out = False
        returncode = 0

    async def _run_snippet(**_k: Any) -> _Result:
        return _Result()

    monkeypatch.setattr(tool_proxy, "get_tool_proxy", lambda: proxy)
    monkeypatch.setattr(code_exec_rpc, "ToolRpcServer", _Server)
    monkeypatch.setattr(code_exec, "run_snippet", _run_snippet)
    monkeypatch.setattr("robothor.engine.code_exec_guards.harden_this_process", lambda: None)
    monkeypatch.setattr(code_exec, "recorded_http_calls", lambda _d: [])
    monkeypatch.setattr(code_exec, "recorded_spawns", lambda _d: [])
    monkeypatch.setattr(code_exec, "shape", lambda result, **_k: {"stdout": result.stdout})

    ctx = type("_Ctx", (), {"run_id": run_id, "workspace": "", "agent_id": "example-agent"})()
    result = await code_exec._execute_code({"code": "print(1)"}, ctx)

    assert result["executed_on"] == "host"
    assert "container" in result["execution_note"]
    rows = [e for e in events if e.get("guardrail_name") == "exec_location"]
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "execute_code"
    assert "executed_on=host" in rows[0]["reason"]
    assert "live container" in rows[0]["reason"]
