"""A snippet's ``tool_call`` is judged as the call it wraps.

The execute_code proxy's allow-set includes the deferred reach-set, and that
set includes ``tool_call``. Every name check in the proxy read the raw name, so
``tool_call(name="spawn_agent", ...)`` walked past the denylist that refuses
``spawn_agent`` itself, a wrapped approval-gated tool never counted toward the
one-approval-per-snippet cap, and the post-execution guardrails were told the
result came from ``tool_call``. Each check now looks through the wrapper,
exactly as a turn's recorder does. No test here starts a kernel.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.code_exec_rpc import ToolRpcServer
from robothor.engine.models import AgentConfig, DeliveryMode
from robothor.engine.session import AgentSession
from robothor.engine.tool_proxy import RunToolProxy
from robothor.engine.tool_turn import ToolTurnRequest

ALLOWED = frozenset({"read_file", "spawn_agent", "ask_user", "execute_code", "tool_call"})


class _FakeProxy:
    max_calls = 10
    calls_made = 0
    allowed = ALLOWED

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        return {"ok": True}


def _rpc() -> tuple[ToolRpcServer, _FakeProxy]:
    proxy = _FakeProxy()
    return ToolRpcServer(directory="/nonexistent", proxy=proxy, max_calls=10), proxy


async def _send(server: ToolRpcServer, name: str, args: dict[str, Any]) -> dict[str, Any]:
    line = json.dumps({"token": server.token, "op": "call", "name": name, "args": args})
    return await server._respond(line.encode())


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", ["spawn_agent", "ask_user", "execute_code"])
async def test_a_wrapped_denied_tool_is_refused_by_the_denylist(denied: str) -> None:
    server, proxy = _rpc()
    reply = await _send(server, "tool_call", {"name": denied, "arguments": {}})
    assert reply["ok"] is False
    assert reply["code"] == "denied"
    assert f"'{denied}'" in reply["error"]
    assert proxy.calls == []


@pytest.mark.asyncio
async def test_a_wrapped_ordinary_tool_still_reaches_the_proxy() -> None:
    server, proxy = _rpc()
    reply = await _send(server, "tool_call", {"name": "read_file", "arguments": {"path": "a"}})
    assert reply == {"ok": True, "result": {"ok": True}}
    assert proxy.calls == [("tool_call", {"name": "read_file", "arguments": {"path": "a"}})]


@pytest.fixture
def runner(engine_config: Any) -> Any:
    from robothor.engine.runner import AgentRunner

    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.execute = AsyncMock(return_value={"ok": True})
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


def _proxy(runner: Any, *, approval: list[str], max_approvals: int = 1) -> RunToolProxy:
    config = AgentConfig(
        id="proxy-agent",
        name="Proxy Agent",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        delivery_mode=DeliveryMode.NONE,
    )
    config.human_approval_tools = approval
    req = ToolTurnRequest(
        assistant_msg=MagicMock(tool_calls=[]),
        session=AgentSession(agent_id=config.id),
        agent_config=config,
        iteration=0,
        allowed_tool_set=ALLOWED,
    )
    return RunToolProxy(
        runner=runner, req=req, allowed=ALLOWED, max_calls=10, max_approvals=max_approvals
    )


@pytest.mark.asyncio
async def test_a_wrapped_approval_gated_tool_counts_toward_the_cap(runner: Any) -> None:
    proxy = _proxy(runner, approval=["read_*"], max_approvals=0)
    result = await proxy.call("tool_call", {"name": "read_file", "arguments": {"path": "a"}})
    assert result["guard"] == "execute_code_approval_cap"
    assert "'read_file'" in result["error"]
    runner.registry.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_execution_guardrails_are_told_the_wrapped_tool(runner: Any) -> None:
    seen: list[str] = []

    def guard(_session: Any, _engine: Any, *, tool_name: str, result: Any, **_kw: Any) -> Any:
        seen.append(tool_name)
        return result

    proxy = _proxy(runner, approval=[])
    with patch("robothor.engine.post_execution.apply_post_execution_guardrails", guard):
        await proxy.call("tool_call", {"name": "read_file", "arguments": {"path": "a"}})
    assert seen == ["read_file"]
