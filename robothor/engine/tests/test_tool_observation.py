"""Trusted tool observers see authorized native results within their owning scope."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
async def test_observation_closes_inherited_tasks_and_copies_handler_data():
    from robothor.engine.tool_observation import observe_tool_result, tool_observation_scope
    from robothor.engine.tools.dispatch import ToolContext

    seen = []
    ctx = ToolContext(run_id="child", tenant_id="tenant-a")
    result = {"content": "Original"}
    go = asyncio.Event()

    def capture(name, args, result, ctx):
        seen.append((name, args, result, ctx))
        result["content"] = "Observer mutation"

    async def escaped():
        await go.wait()
        observe_tool_result("web_fetch", {}, result, ctx)

    with tool_observation_scope(capture, names={"web_fetch"}):
        observe_tool_result("web_fetch", {"url": "https://example.com"}, result, ctx)
        observe_tool_result("unrelated", {}, result, ctx)
        task = asyncio.create_task(escaped())
    go.set()
    await task
    assert len(seen) == 1
    assert seen[0][3].run_id == "child"
    assert result == {"content": "Original"}


@pytest.mark.asyncio
async def test_dispatch_observes_native_result_after_permission_and_before_annotation(monkeypatch):
    from robothor.engine.tool_observation import tool_observation_scope
    from robothor.engine.tools import dispatch, verification

    permission = Mock(return_value=None)
    monkeypatch.setattr("robothor.engine.permissions.check_tool_permission", permission)
    monkeypatch.setattr(
        "robothor.engine.tools.get_registry", lambda: Mock(get_adapter_route=lambda n: None)
    )
    handler = AsyncMock(return_value={"content": "Fetched", "status": 200})
    monkeypatch.setattr(dispatch, "_get_handlers", lambda: {"web_fetch": handler})
    monkeypatch.setattr(dispatch, "_audit_tool_call", Mock())
    monkeypatch.setattr("robothor.engine.repeat_guard.guard_for_run", lambda run: None)
    monkeypatch.setattr(
        verification, "verify_tool_result", AsyncMock(return_value={"annotated": True})
    )
    seen = []
    with tool_observation_scope(lambda *args: seen.append(args), names={"web_fetch"}):
        assert await dispatch._execute_tool("web_fetch", {}, run_id="child") == {"annotated": True}
        permission.return_value = "Denied"
        assert "error" in await dispatch._execute_tool("web_fetch", {}, run_id="denied")
    assert len(seen) == 1
    assert seen[0][2] == {"content": "Fetched", "status": 200}
    assert seen[0][3].run_id == "child"
    assert handler.await_count == 1
