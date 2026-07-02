"""Tests for deferred/searchable tool loading (Rip 16 / G4 — tools-as-code).

Covers: registry deferral decision + schema reduction, the search/describe
helpers, and the tool_search/tool_describe/tool_call meta-tool handlers —
including the allow-list enforcement that prevents tool_call from reaching a
tool outside the agent's allow-list when deferral shrinks the advertised set.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.tools.constants import CORE_TOOLS, TOOLSEARCH_TOOLS
from robothor.engine.tools.dispatch import (
    ToolContext,
    clear_deferred_allowed,
    set_deferred_allowed,
)
from robothor.engine.tools.handlers import toolsearch
from robothor.engine.tools.registry import ToolRegistry


def _names(schemas: list[dict]) -> set[str]:
    return {s["function"]["name"] for s in schemas}


def _cfg(allowed=None, denied=None):
    """Minimal AgentConfig-like stub for registry filtering."""
    from robothor.engine.models import AgentConfig, DeliveryMode

    return AgentConfig(
        id="t",
        name="t",
        description="",
        model_primary="openrouter/x/model",
        model_fallbacks=[],
        cron_expr="0 * * * *",
        timezone="UTC",
        timeout_seconds=30,
        delivery_mode=DeliveryMode.NONE,
        tools_allowed=allowed or [],
        tools_denied=denied or [],
        instruction_file="",
        bootstrap_files=[],
    )


@pytest.fixture
def registry():
    return ToolRegistry()


class TestDeferralDecision:
    def test_off_by_default(self, registry):
        # Flag defaults off → never defers, even for a broad-access agent.
        assert registry.should_defer(_cfg()) is False

    def test_defers_broad_agent_when_enabled(self, registry, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RIP_16_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_DEFERRED_TOOLS_THRESHOLD", "5")
        # No allow-list → all tools → well over threshold.
        assert registry.should_defer(_cfg()) is True

    def test_small_curated_agent_not_deferred(self, registry, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RIP_16_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_DEFERRED_TOOLS_THRESHOLD", "40")
        cfg = _cfg(allowed=["read_file", "list_directory", "search_memory"])
        assert registry.should_defer(cfg) is False


class TestSchemaReduction:
    def test_meta_tools_excluded_from_normal_set(self, registry):
        names = set(registry.get_tool_names(_cfg()))
        assert not (names & TOOLSEARCH_TOOLS)

    def test_non_deferred_returns_full_no_meta(self, registry):
        advertised = _names(registry.build_for_agent(_cfg()))
        assert not (advertised & TOOLSEARCH_TOOLS)
        assert len(advertised) > 40  # broad agent, full set

    def test_deferred_returns_core_plus_meta(self, registry, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RIP_16_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_DEFERRED_TOOLS_THRESHOLD", "5")
        advertised = _names(registry.build_for_agent(_cfg()))
        # Meta-tools present, set is small, and it's a subset of core ∪ meta.
        assert advertised >= TOOLSEARCH_TOOLS
        assert advertised <= (CORE_TOOLS | TOOLSEARCH_TOOLS)
        assert len(advertised) < 40

    def test_deferred_whitelist_is_full_set_plus_meta(self, registry, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RIP_16_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_DEFERRED_TOOLS_THRESHOLD", "5")
        wl = registry.deferred_whitelist(_cfg())
        assert wl >= TOOLSEARCH_TOOLS
        # Contains far more than the advertised core (the full allowed set).
        assert len(wl) > len(_names(registry.build_for_agent(_cfg())))


class TestSearchAndDescribe:
    def test_search_ranks_name_matches_first(self, registry):
        names = registry.get_tool_names(_cfg())
        results = registry.search_tools(names, "memory", limit=5)
        assert results
        assert any("memory" in r["name"] for r in results)

    def test_get_schema_known_and_unknown(self, registry):
        assert registry.get_schema("read_file") is not None
        assert registry.get_schema("does_not_exist") is None


class TestMetaToolHandlers:
    @pytest.fixture
    def deferred_ctx(self):
        # Install an allow-set: a couple of real tools + the meta-tools.
        token = set_deferred_allowed(frozenset({"gws_gmail_send", "read_file"}) | TOOLSEARCH_TOOLS)
        yield ToolContext(agent_id="t", run_id="r", tenant_id="robothor-primary")
        clear_deferred_allowed(token)

    @pytest.mark.asyncio
    async def test_search_requires_deferred_run(self):
        # No allow-set installed → tool_search refuses.
        out = await toolsearch._tool_search({"query": "email"}, ToolContext())
        assert "error" in out

    @pytest.mark.asyncio
    async def test_search_returns_results(self, deferred_ctx):
        out = await toolsearch._tool_search({"query": "gmail"}, deferred_ctx)
        assert "results" in out
        assert any(r["name"] == "gws_gmail_send" for r in out["results"])

    @pytest.mark.asyncio
    async def test_describe_allowed_tool(self, deferred_ctx):
        out = await toolsearch._tool_describe({"name": "gws_gmail_send"}, deferred_ctx)
        assert out.get("name") == "gws_gmail_send"
        assert "parameters" in out

    @pytest.mark.asyncio
    async def test_describe_rejects_out_of_allowlist(self, deferred_ctx):
        out = await toolsearch._tool_describe({"name": "exec"}, deferred_ctx)
        assert "error" in out  # exec not in this run's allow-set

    @pytest.mark.asyncio
    async def test_call_forwards_to_registry_for_allowed_tool(self, deferred_ctx):
        with patch(
            "robothor.engine.tools.registry.ToolRegistry.execute",
            new=AsyncMock(return_value={"ok": True}),
        ) as ex:
            out = await toolsearch._tool_call(
                {"name": "gws_gmail_send", "arguments": {"to": "bob@example.com"}}, deferred_ctx
            )
        assert out == {"ok": True}
        assert ex.call_args.args[0] == "gws_gmail_send"

    @pytest.mark.asyncio
    async def test_call_refuses_out_of_allowlist(self, deferred_ctx):
        # exec is NOT in this run's allow-set → must be refused WITHOUT dispatch.
        with patch(
            "robothor.engine.tools.registry.ToolRegistry.execute",
            new=AsyncMock(return_value={"ok": True}),
        ) as ex:
            out = await toolsearch._tool_call(
                {"name": "exec", "arguments": {"command": "rm -rf /"}}, deferred_ctx
            )
        assert "error" in out
        ex.assert_not_called()

    @pytest.mark.asyncio
    async def test_call_refuses_meta_recursion(self, deferred_ctx):
        out = await toolsearch._tool_call({"name": "tool_search", "arguments": {}}, deferred_ctx)
        assert "error" in out
