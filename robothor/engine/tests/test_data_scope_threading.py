"""Tests that the run's IdentityContext reaches tool handlers (Task 5).

Data scoping (``robothor.identity.scope``) needs to know who is calling —
the resolved ``IdentityContext`` the runner already stashes on
``session.identity`` (Task 2). This pins the plumbing that carries it from
``ToolRegistry.execute`` → ``_execute_tool`` → ``ToolContext.identity``,
without which handlers have no way to compute a DataScope at all.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.tools import dispatch
from robothor.identity import IdentityContext


@pytest.mark.asyncio
async def test_tool_context_defaults_identity_to_none() -> None:
    """Every pre-existing caller that never passes identity= is unaffected."""
    captured: dict[str, Any] = {}

    async def _capture(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        captured["identity"] = ctx.identity
        return {"ok": True}

    with patch.object(dispatch, "_get_handlers", return_value={"probe": _capture}):
        await dispatch._execute_tool("probe", {}, user_role="service")

    assert captured["identity"] is None


@pytest.mark.asyncio
async def test_execute_tool_threads_identity_into_tool_context() -> None:
    identity = IdentityContext(
        tenant_id="tenant-a",
        channel="webchat",
        identifier="user-1",
        verified=True,
        role="member",
        person_id="person-1",
    )
    captured: dict[str, Any] = {}

    async def _capture(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        captured["identity"] = ctx.identity
        return {"ok": True}

    with patch.object(dispatch, "_get_handlers", return_value={"probe": _capture}):
        await dispatch._execute_tool("probe", {}, user_role="member", identity=identity)

    assert captured["identity"] is identity


@pytest.mark.asyncio
async def test_registry_execute_threads_identity_through(monkeypatch) -> None:
    from robothor.engine.tools.registry import ToolRegistry

    identity = IdentityContext(
        tenant_id="tenant-a",
        channel="telegram",
        identifier="tg-1",
        verified=True,
        role="viewer",
        person_id="person-2",
    )
    seen: dict[str, Any] = {}

    async def _fake_execute_tool(name: str, args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr("robothor.engine.tools.registry._execute_tool", _fake_execute_tool)

    registry = ToolRegistry.__new__(ToolRegistry)
    result = await ToolRegistry.execute(
        registry, "probe", {}, user_role="viewer", identity=identity, timeout=0
    )

    assert result == {"ok": True}
    assert seen["identity"] is identity
