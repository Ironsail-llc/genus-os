"""A tool that declares its OWN ``tenant`` or ``dry_run`` gets it untouched.

``cross_tenant.admit`` gives ``tenant`` (and ``dry_run`` on a write) a meaning
only for the engine's allowlisted built-ins. An external MCP server's tool or
a plugin tool may declare a parameter of the same name for its own purposes;
refusing it, stripping it or auditing it as a crossing would break that tool.
A built-in tool that never advertised ``tenant`` is still refused: its handler
would ignore the argument and answer about the home tenant.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import TriggerType

PARENT, CHILD_A, CHILD_B = "acme-parent", "acme-child-a", "acme-child-b"
OWNER_REACH = (PARENT, CHILD_A, CHILD_B)

ADAPTER_TOOL = "acme_billing_lookup"
PLUGIN_TOOL = "acme_plugin_report"


def _schema(name: str, *params: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {
                "type": "object",
                "properties": {p: {"type": "string"} for p in params},
            },
        },
    }


class _Registry:
    """The two things dispatch asks the tool registry: route, and the schema."""

    def __init__(self) -> None:
        from robothor.engine.tools.registry import ToolRegistry

        self.real = ToolRegistry.__new__(ToolRegistry)
        self.real._schemas = {
            ADAPTER_TOOL: _schema(ADAPTER_TOOL, "tenant", "dry_run", "account"),
            PLUGIN_TOOL: _schema(PLUGIN_TOOL, "tenant", "dry_run"),
            "write_file": _schema("write_file", "path", "content"),
        }
        self.real._adapter_routes = {ADAPTER_TOOL: "acme-billing"}

    def __getattr__(self, attr: str) -> Any:
        return getattr(self.real, attr)


@pytest.fixture
def env():
    calls: list[dict[str, Any]] = []

    def handler(name: str):
        async def _h(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            calls.append({"name": name, "args": dict(args), "tenant_id": ctx.tenant_id})
            return {"ok": True}

        return _h

    handlers = {n: handler(n) for n in (PLUGIN_TOOL, "write_file", "list_tasks", "create_task")}
    adapter_session = MagicMock()

    async def _call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append({"name": name, "args": dict(args), "adapter": True})
        return {"ok": True}

    adapter_session.call_tool = _call_tool
    pool = MagicMock()
    pool.get_session = AsyncMock(return_value=adapter_session)
    session = SimpleNamespace(
        run=SimpleNamespace(
            parent_run_id=None,
            nesting_depth=0,
            trigger_type=TriggerType.WEBCHAT,
            is_benchmark=False,
        )
    )
    with (
        patch("robothor.engine.permissions.check_tool_permission", return_value=None),
        patch("robothor.engine.tools.dispatch._runtime_denial", AsyncMock(return_value=None)),
        patch("robothor.engine.tools.dispatch._get_handlers", return_value=handlers),
        patch("robothor.engine.tools.get_registry", return_value=_Registry()),
        patch("robothor.engine.mcp_client.get_mcp_client_pool", return_value=pool),
        patch("robothor.engine.tools.dispatch._audit_tool_call"),
        patch("robothor.engine.session_registry.lookup", return_value=session),
        patch("robothor.engine.tools.cross_tenant.audit_cross_tenant") as audit,
    ):
        yield SimpleNamespace(calls=calls, audit=audit)


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from robothor.engine.tools.dispatch import _execute_tool

    return asyncio.run(
        _execute_tool(
            name,
            args,
            agent_id="main",
            run_id="run-1",
            tenant_id=PARENT,
            user_id="alice",
            user_role="owner",
            accessible_tenant_ids=OWNER_REACH,
        )
    )


@pytest.mark.parametrize("value", ["someone-elses-tenant", CHILD_A, 42, ""])
def test_an_adapter_tools_own_tenant_reaches_it_unchanged(env, value):
    args = {"tenant": value, "account": "A-1"}
    result = _call(ADAPTER_TOOL, args)

    assert result.get("refused") is not True, result
    (call,) = env.calls
    assert call == {"name": ADAPTER_TOOL, "args": args, "adapter": True}
    env.audit.assert_not_called()


@pytest.mark.parametrize("value", ["yes", True, None])
def test_an_adapter_tools_own_dry_run_reaches_it_unchanged(env, value):
    args = {"dry_run": value, "account": "A-1"}
    result = _call(ADAPTER_TOOL, args)

    assert result.get("refused") is not True, result
    (call,) = env.calls
    assert call["args"] == args
    env.audit.assert_not_called()


def test_a_plugin_tool_that_declares_tenant_gets_it_unchanged(env):
    args = {"tenant": "their-own-meaning", "dry_run": "maybe"}
    result = _call(PLUGIN_TOOL, args)

    assert result.get("refused") is not True, result
    (call,) = env.calls
    assert call["args"] == args
    assert call["tenant_id"] == PARENT, "no tenant swap: the engine did not act on it"
    env.audit.assert_not_called()


def test_a_builtin_that_never_declared_tenant_is_still_refused(env):
    result = _call("write_file", {"tenant": CHILD_A, "path": "x"})

    assert result.get("refused") is True
    assert "cannot act in another tenant" in str(result["error"])
    assert env.calls == []
    assert env.audit.call_args.kwargs["outcome"] == "denied"


def test_an_allowlisted_read_still_crosses(env):
    result = _call("list_tasks", {"tenant": CHILD_A, "status": "TODO"})

    assert "error" not in result, result
    (call,) = env.calls
    assert call["args"] == {"status": "TODO"}
    assert call["tenant_id"] == CHILD_A
    assert env.audit.call_args.kwargs["outcome"] == "ok"


def test_an_allowlisted_write_still_refuses_a_non_boolean_dry_run(env):
    result = _call("create_task", {"title": "x", "dry_run": "true"})

    assert "dry_run must be true or false" in str(result["error"])
    assert env.calls == []


def test_the_registry_reports_a_tools_own_parameters():
    from robothor.engine.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg._schemas = {PLUGIN_TOOL: _schema(PLUGIN_TOOL, "tenant")}
    reg._adapter_routes = {}

    assert reg.declared_parameters(PLUGIN_TOOL) == frozenset({"tenant"})
    assert reg.declared_parameters("no_such_tool") == frozenset()


def _answered(name: str, args: dict[str, Any], outcome: str) -> None:
    from robothor.engine.tools.cross_tenant import audit_answered

    ctx = SimpleNamespace(tenant_id=PARENT, user_id="alice", agent_id="main")
    audit_answered(name, args, ctx, outcome)


@pytest.mark.parametrize("tool", [ADAPTER_TOOL, PLUGIN_TOOL])
@pytest.mark.parametrize("outcome", ["replay", "repeat"])
def test_a_replayed_call_with_the_tools_own_tenant_is_not_audited_as_a_crossing(env, tool, outcome):
    """The effect ledger replays any write, an external tool's included. Its
    ``tenant`` is that tool's own word: no other tenant was entered, so the
    audit trail must not say one was."""
    _answered(tool, {"tenant": "their-own-meaning"}, outcome)

    env.audit.assert_not_called()


def test_a_replayed_crossing_by_an_allowlisted_tool_is_still_audited(env):
    _answered("create_task", {"tenant": "acme-child-a", "title": "x"}, "replay")

    assert env.audit.call_count == 1
    assert env.audit.call_args.kwargs["target_tenant"] == "acme-child-a"
    assert env.audit.call_args.kwargs["outcome"] == "replay"
