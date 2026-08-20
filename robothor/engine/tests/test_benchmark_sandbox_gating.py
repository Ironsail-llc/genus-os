"""Benchmark sandbox gating for CRM and memory side effects.

The is_benchmark flag was enforced only for gws mutating tools
(handlers/gws.py:_GWS_MUTATING_TOOLS), so a benchmark run could file real
CRM tasks, send operator notifications, and write memory facts. In
production this materialized a benchmark fixture ("unknown person at
2:47 AM") as a real high-priority requires_human escalation that the
morning briefing reported as an intruder, with earlier benchmark leaks in
memory_facts supplying a fake "recurring pattern".

These tests pin the fix: with ctx.is_benchmark=True every task-mutating
CRM tool, operator notification, and memory mutation refuses with a
structured error (mirroring the gws precedent), while non-benchmark
behavior is byte-for-byte unchanged. The session-goal DAL path is gated
via a ContextVar that tool dispatch sets for benchmark calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.tools import dispatch
from robothor.engine.tools.dispatch import ToolContext

CRM_GATED_TOOLS = [
    "create_task",
    "update_task",
    "resolve_task",
    "delete_task",
    "approve_task",
    "reject_task",
    "send_notification",
]

MEMORY_GATED_TOOLS = [
    "store_memory",
    "append_to_block",
    "memory_block_write",
    "record_resolution",
]


def _refusal(tool: str) -> dict[str, str]:
    return {
        "error": f"benchmark sandbox: {tool} writes are disabled",
        "guard": "is_benchmark",
    }


class TestCrmHandlersRefuseInBenchmarkMode:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", CRM_GATED_TOOLS)
    async def test_mutating_crm_tool_refuses(self, tool: str) -> None:
        """Benchmark ctx → structured refusal, before any DB/DAL work.

        Exact-dict assertion is deliberate: if the guard were missing the
        handler would hit the DAL (raising or returning a different shape),
        so equality proves the refusal happened first.
        """
        from robothor.engine.tools.handlers.crm import HANDLERS

        ctx = ToolContext(agent_id="benchmark-agent", is_benchmark=True)
        result = await HANDLERS[tool]({"id": "x", "title": "t"}, ctx)
        assert result == _refusal(tool)


class TestMemoryHandlersRefuseInBenchmarkMode:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", MEMORY_GATED_TOOLS)
    async def test_mutating_memory_tool_refuses(self, tool: str) -> None:
        from robothor.engine.tools.handlers.memory import HANDLERS

        ctx = ToolContext(agent_id="benchmark-agent", is_benchmark=True)
        result = await HANDLERS[tool]({"content": "c", "block_name": "b"}, ctx)
        assert result == _refusal(tool)


class TestNonBenchmarkBehaviorUnchanged:
    @pytest.mark.asyncio
    async def test_create_task_still_writes(self) -> None:
        from robothor.engine.tools.handlers.crm import HANDLERS

        ctx = ToolContext(agent_id="main", tenant_id="t1")
        with patch("robothor.crm.dal.create_task", return_value="task-123") as mock_create:
            result = await HANDLERS["create_task"]({"title": "Real task"}, ctx)
        assert result == {"id": "task-123", "title": "Real task"}
        assert mock_create.called

    @pytest.mark.asyncio
    async def test_resolve_task_still_resolves(self) -> None:
        from robothor.engine.tools.handlers.crm import HANDLERS

        ctx = ToolContext(agent_id="main", tenant_id="t1")
        with (
            patch("robothor.crm.dal.get_task", return_value=None),
            patch("robothor.crm.dal.resolve_task", return_value=True) as mock_resolve,
        ):
            result = await HANDLERS["resolve_task"]({"id": "task-1", "resolution": "done"}, ctx)
        assert result == {"success": True, "id": "task-1"}
        assert mock_resolve.called

    @pytest.mark.asyncio
    async def test_store_memory_still_stores(self) -> None:
        from robothor.engine.tools.handlers import memory as memory_mod

        ctx = ToolContext(agent_id="main", tenant_id="t1")

        async def _fake_store(content: str, content_type: str, *, tenant_id: str = "") -> dict:
            return {"id": 1, "facts_stored": 1}

        with (
            patch("robothor.memory.write_jobs.async_write_enabled", return_value=False),
            patch.object(memory_mod, "store_memory_content", side_effect=_fake_store) as mock_store,
        ):
            result = await memory_mod.HANDLERS["store_memory"]({"content": "a fact"}, ctx)
        assert result == {"id": 1, "facts_stored": 1}
        assert mock_store.called

    @pytest.mark.asyncio
    async def test_append_to_block_still_appends(self) -> None:
        from robothor.engine.tools.handlers.memory import HANDLERS

        ctx = ToolContext(agent_id="main", tenant_id="t1")
        with patch("robothor.crm.dal.append_to_block", return_value=True) as mock_append:
            result = await HANDLERS["append_to_block"]({"block_name": "b", "entry": "e"}, ctx)
        assert result == {"success": True, "block_name": "b"}
        assert mock_append.called

    @pytest.mark.asyncio
    async def test_read_tools_not_gated_in_benchmark_mode(self) -> None:
        """Reads stay allowed in benchmark mode — mirrors the gws precedent."""
        from robothor.engine.tools.handlers.crm import HANDLERS

        ctx = ToolContext(agent_id="benchmark-agent", tenant_id="t1", is_benchmark=True)
        with patch("robothor.crm.dal.get_task", return_value={"id": "task-1", "title": "t"}):
            result = await HANDLERS["get_task"]({"id": "task-1"}, ctx)
        assert "guard" not in result


class TestDispatchThreadsBenchmarkSandboxToDal:
    @pytest.mark.asyncio
    async def test_sandbox_contextvar_set_during_benchmark_dispatch(self) -> None:
        from robothor.crm import dal

        seen: dict[str, bool] = {}

        async def _probe(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            seen["active"] = dal.benchmark_sandbox_active()
            return {"ok": True}

        with patch.object(dispatch, "_get_handlers", return_value={"probe": _probe}):
            await dispatch._execute_tool("probe", {}, user_role="service", is_benchmark=True)
        assert seen["active"] is True
        # And it must not leak past the dispatch call.
        assert dal.benchmark_sandbox_active() is False

    @pytest.mark.asyncio
    async def test_sandbox_contextvar_unset_for_normal_dispatch(self) -> None:
        from robothor.crm import dal

        seen: dict[str, bool] = {}

        async def _probe(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            seen["active"] = dal.benchmark_sandbox_active()
            return {"ok": True}

        with patch.object(dispatch, "_get_handlers", return_value={"probe": _probe}):
            await dispatch._execute_tool("probe", {}, user_role="service")
        assert seen["active"] is False

    @pytest.mark.asyncio
    async def test_create_goal_refused_end_to_end_in_benchmark_mode(self) -> None:
        """Full path: dispatch → goal handler → session_goal → dal refusal.

        The session-goal machinery is how the production leak surfaced (a
        benchmark run filed a session_goal/thread-tagged escalation task), so
        this pins the whole chain, not just the dal unit.
        """
        with patch("robothor.crm.dal.get_active_session_goal", return_value=None):
            result = await dispatch._execute_tool(
                "create_goal",
                {"objective": "synthetic benchmark goal"},
                user_role="service",
                is_benchmark=True,
            )
        assert "benchmark sandbox" in result.get("error", "")
