"""Tests for hierarchical tenant access resolution (Phase 4).

Covers:
- resolve_accessible_tenants() with various roles
- accessible_tenant_ids field on AgentRun
- ToolContext receives accessible_tenant_ids
- registry.execute passes accessible_tenant_ids through dispatch
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.models import AgentRun
from robothor.engine.permissions import resolve_accessible_tenants
from robothor.engine.tools.dispatch import ToolContext

# ── resolve_accessible_tenants ──────────────────────────────────────


class TestResolveAccessibleTenants:
    """Unit tests for the tenant hierarchy resolver."""

    def test_none_role_returns_own_tenant(self):
        result = resolve_accessible_tenants("t1", None)
        assert result == ("t1",)

    def test_member_returns_own_tenant(self):
        result = resolve_accessible_tenants("t1", "member")
        assert result == ("t1",)

    def test_viewer_returns_own_tenant(self):
        result = resolve_accessible_tenants("t1", "viewer")
        assert result == ("t1",)

    @patch("robothor.engine.permissions._get_child_tenants")
    def test_owner_gets_children(self, mock_children):
        mock_children.side_effect = lambda tid: {
            "parent": ["child-a", "child-b"],
        }.get(tid, [])

        result = resolve_accessible_tenants("parent", "owner")
        assert "parent" in result
        assert "child-a" in result
        assert "child-b" in result
        assert len(result) == 3

    @patch("robothor.engine.permissions._get_child_tenants")
    def test_admin_gets_children(self, mock_children):
        mock_children.side_effect = lambda tid: {
            "parent": ["child-a"],
        }.get(tid, [])

        result = resolve_accessible_tenants("parent", "admin")
        assert result == ("parent", "child-a")

    @patch("robothor.engine.permissions._get_child_tenants")
    def test_owner_gets_grandchildren(self, mock_children):
        mock_children.side_effect = lambda tid: {
            "root": ["mid"],
            "mid": ["leaf"],
        }.get(tid, [])

        result = resolve_accessible_tenants("root", "owner")
        assert result == ("root", "mid", "leaf")

    @patch("robothor.engine.permissions._get_child_tenants")
    def test_max_depth_caps_traversal(self, mock_children):
        # Build a chain: t0 -> t1 -> t2 -> t3 -> t4
        mock_children.side_effect = lambda tid: {
            "t0": ["t1"],
            "t1": ["t2"],
            "t2": ["t3"],
            "t3": ["t4"],
        }.get(tid, [])

        result = resolve_accessible_tenants("t0", "owner", max_depth=2)
        # depth=0: t0->t1, depth=1: t1->t2, stop at max_depth=2
        assert "t0" in result
        assert "t1" in result
        assert "t2" in result
        assert "t3" not in result

    @patch("robothor.engine.permissions._get_child_tenants")
    def test_no_duplicates_in_diamond(self, mock_children):
        # Diamond: root -> [a, b], a -> [shared], b -> [shared]
        mock_children.side_effect = lambda tid: {
            "root": ["a", "b"],
            "a": ["shared"],
            "b": ["shared"],
        }.get(tid, [])

        result = resolve_accessible_tenants("root", "owner")
        assert result.count("shared") == 1

    @patch("robothor.engine.permissions._get_child_tenants")
    def test_db_failure_degrades_to_own_tenant(self, mock_children):
        mock_children.side_effect = Exception("DB down")
        # Even though owner, DB failure means no children discovered
        result = resolve_accessible_tenants("t1", "owner")
        assert result == ("t1",)

    def test_empty_role_string_returns_own(self):
        result = resolve_accessible_tenants("t1", "")
        assert result == ("t1",)


# ── AgentRun.accessible_tenant_ids ──────────────────────────────────


class TestAgentRunAccessibleTenants:
    """Verify the field exists and defaults correctly."""

    def test_default_is_empty_tuple(self):
        run = AgentRun()
        assert run.accessible_tenant_ids == ()

    def test_can_set_tuple(self):
        run = AgentRun()
        run.accessible_tenant_ids = ("t1", "t2")
        assert run.accessible_tenant_ids == ("t1", "t2")


# ── ToolContext.accessible_tenant_ids ───────────────────────────────


class TestToolContextAccessibleTenants:
    """Verify ToolContext carries accessible_tenant_ids."""

    def test_default_is_empty_tuple(self):
        ctx = ToolContext(agent_id="a", tenant_id="t1")
        assert ctx.accessible_tenant_ids == ()

    def test_set_via_constructor(self):
        ctx = ToolContext(
            agent_id="a",
            tenant_id="t1",
            accessible_tenant_ids=("t1", "t2"),
        )
        assert ctx.accessible_tenant_ids == ("t1", "t2")


# ── Registry threading ──────────────────────────────────────────────


class TestRegistryThreading:
    """Verify registry.execute passes accessible_tenant_ids to dispatch."""

    @pytest.mark.asyncio
    async def test_execute_passes_accessible_tenants(self):
        from robothor.engine.tools.registry import ToolRegistry

        registry = ToolRegistry()
        tenants = ("t1", "child-a")

        with patch("robothor.engine.tools.registry._execute_tool") as mock_exec:
            mock_exec.return_value = {"ok": True}

            await registry.execute(
                "test_tool",
                {},
                agent_id="agent-1",
                tenant_id="t1",
                workspace="/tmp",
                accessible_tenant_ids=tenants,
            )

            mock_exec.assert_called_once()
            call_kwargs = mock_exec.call_args
            assert call_kwargs.kwargs.get("accessible_tenant_ids") == tenants

    @pytest.mark.asyncio
    async def test_execute_defaults_empty_accessible_tenants(self):
        from robothor.engine.tools.registry import ToolRegistry

        registry = ToolRegistry()

        with patch("robothor.engine.tools.registry._execute_tool") as mock_exec:
            mock_exec.return_value = {"ok": True}

            await registry.execute(
                "test_tool",
                {},
                agent_id="agent-1",
                tenant_id="t1",
                workspace="/tmp",
            )

            call_kwargs = mock_exec.call_args
            assert call_kwargs.kwargs.get("accessible_tenant_ids") == ()


# ── Dispatch threading ──────────────────────────────────────────────


class TestDispatchThreading:
    """Verify _execute_tool builds ToolContext with accessible_tenant_ids."""

    @pytest.mark.asyncio
    async def test_dispatch_builds_context_with_accessible_tenants(self):
        from robothor.engine.tools.dispatch import _execute_tool

        tenants = ("t1", "t2")

        # Mock the handler lookup to capture the ToolContext
        captured_ctx = {}

        async def fake_handler(args, ctx):
            captured_ctx["ctx"] = ctx
            return {"ok": True}

        mock_registry = MagicMock()
        mock_registry.get_adapter_route.return_value = None

        with (
            patch("robothor.engine.tools.dispatch._get_handlers") as mock_handlers,
            patch("robothor.engine.tools.get_registry", return_value=mock_registry),
        ):
            mock_handlers.return_value = {"my_tool": fake_handler}

            await _execute_tool(
                "my_tool",
                {},
                agent_id="agent-1",
                tenant_id="t1",
                workspace="/tmp",
                accessible_tenant_ids=tenants,
            )

        assert captured_ctx["ctx"].accessible_tenant_ids == tenants
