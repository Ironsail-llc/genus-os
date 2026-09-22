from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from robothor.engine import host_execution, session_registry
from robothor.engine.tools.dispatch import ToolContext
from robothor.identity import IdentityContext


@pytest.fixture
def owner(monkeypatch):
    identity = IdentityContext(
        tenant_id="test", channel="telegram", identifier="owner", verified=True, role="owner"
    )
    ctx = ToolContext(
        agent_id="main", run_id="run-test", user_id="owner", tenant_id="test", identity=identity
    )
    session = SimpleNamespace(
        readonly_mode=False, run=SimpleNamespace(parent_run_id=None, tools_provided=["exec"])
    )
    monkeypatch.setattr(
        session_registry, "lookup", lambda run_id: session if run_id == ctx.run_id else None
    )
    return ctx, session


def test_only_owner_main_non_plan_root_run_can_use_host(owner):
    ctx, session = owner
    assert host_execution.eligible(ctx)
    assert not host_execution.eligible(replace(ctx, agent_id="worker"))
    assert not host_execution.eligible(replace(ctx, is_benchmark=True))
    assert not host_execution.eligible(replace(ctx, identity=replace(ctx.identity, verified=False)))
    assert not host_execution.eligible(replace(ctx, identity=replace(ctx.identity, role="member")))
    assert not host_execution.eligible(replace(ctx, run_id="missing"))
    session.readonly_mode = True
    assert not host_execution.eligible(ctx)
    session.readonly_mode = False
    session.run.parent_run_id = "parent"
    assert not host_execution.eligible(ctx)


@pytest.fixture
def signed(monkeypatch):
    from robothor.auth import tokens

    monkeypatch.setattr(tokens, "_signing_key_cache", "test-signing-key-is-at-least-32-bytes-long")
    return lambda body, agent="main": tokens.issue_service_token(
        "owner",
        "test",
        agent_id=agent,
        audience=host_execution.AUDIENCE,
        scopes=["host:exec", host_execution._digest(body)],
    )


async def test_real_command_authentication_and_replay(signed, tmp_path):
    body = {
        "command": "printf host-ok",
        "cwd": str(tmp_path),
        "env": {},
        "timeout": 2,
        "run_id": "test",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host_execution.create_app()), base_url="http://host"
    ) as client:
        assert (await client.post("/exec", json=body)).status_code == 403
        assert (
            await client.post(
                "/exec", json=body, headers={"Authorization": "Bearer " + signed(body, "worker")}
            )
        ).status_code == 403
        token = signed(body)
        headers = {"Authorization": "Bearer " + token}
        assert (
            await client.post("/exec", json={**body, "command": "false"}, headers=headers)
        ).status_code == 403
        response = await client.post("/exec", json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["stdout"] == "host-ok"
        assert (await client.post("/exec", json=body, headers=headers)).status_code == 409


async def test_timeout_kills_descendants(signed, tmp_path):
    body = {
        "command": "sleep 20 & wait",
        "cwd": str(tmp_path),
        "env": {},
        "timeout": 1,
        "run_id": "test",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host_execution.create_app()), base_url="http://host"
    ) as client:
        result = await client.post(
            "/exec", json=body, headers={"Authorization": "Bearer " + signed(body)}
        )
        assert result.json()["error"] == "Command timed out"
        assert result.json()["exit_code"] != 0


async def test_plan_only_exec_never_reaches_any_backend(owner, monkeypatch):
    from robothor.engine.tools.handlers.filesystem import _exec

    ctx, session = owner
    session.readonly_mode = True
    result = await _exec({"command": "genus-host deploy main"}, ctx)
    assert result["error_type"] == "readonly_mode"
