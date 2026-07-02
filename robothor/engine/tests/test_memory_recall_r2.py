"""Tests for R2 (#45): cron warmup recall flag + default search_memory narrowing.

The default search_memory path hard-fanned-out (entities+insights+episodes) on
every call — token waste on a narrow lookup. R2 flag-gates that so a default
call is facts-only when MEMORY_NARROW_SEARCH is on; callers opt into fan-out via
args. Cron warmup recall is flag-gated similarly (its full behavior needs a DB,
so here we pin the flag + the gate).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import feature_flags
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import memory as memmod


class TestFlags:
    def test_narrow_search_flag(self, monkeypatch):
        monkeypatch.delenv("MEMORY_NARROW_SEARCH", raising=False)
        assert feature_flags.narrow_memory_search_enabled() is False
        monkeypatch.setenv("MEMORY_NARROW_SEARCH", "1")
        assert feature_flags.narrow_memory_search_enabled() is True

    def test_cron_warmup_recall_flag(self, monkeypatch):
        monkeypatch.delenv("MEMORY_CRON_WARMUP_RECALL", raising=False)
        assert feature_flags.cron_warmup_recall_enabled() is False
        monkeypatch.setenv("MEMORY_CRON_WARMUP_RECALL", "yes")
        assert feature_flags.cron_warmup_recall_enabled() is True


class TestSearchMemoryFanOut:
    @pytest.fixture(autouse=True)
    def _no_router(self, monkeypatch):
        # Keep the default (RIP-15-off) path under test, not the router.
        monkeypatch.delenv("ROBOTHOR_RIP_15_ENABLED", raising=False)

    @pytest.mark.asyncio
    async def test_default_fans_out(self, monkeypatch):
        monkeypatch.delenv("MEMORY_NARROW_SEARCH", raising=False)
        sf = AsyncMock(return_value=[])
        with patch("robothor.memory.facts.search_facts", sf):
            await memmod.HANDLERS["search_memory"]({"query": "x"}, ToolContext(tenant_id="t"))
        kw = sf.call_args.kwargs
        assert kw["expand_entities"] and kw["include_insights"] and kw["include_episodes"]

    @pytest.mark.asyncio
    async def test_narrow_is_facts_only(self, monkeypatch):
        monkeypatch.setenv("MEMORY_NARROW_SEARCH", "1")
        sf = AsyncMock(return_value=[])
        with patch("robothor.memory.facts.search_facts", sf):
            await memmod.HANDLERS["search_memory"]({"query": "x"}, ToolContext(tenant_id="t"))
        kw = sf.call_args.kwargs
        assert not kw["expand_entities"]
        assert not kw["include_insights"]
        assert not kw["include_episodes"]

    @pytest.mark.asyncio
    async def test_caller_can_opt_into_fanout_even_when_narrow(self, monkeypatch):
        monkeypatch.setenv("MEMORY_NARROW_SEARCH", "1")
        sf = AsyncMock(return_value=[])
        with patch("robothor.memory.facts.search_facts", sf):
            await memmod.HANDLERS["search_memory"](
                {"query": "x", "include_episodes": True}, ToolContext(tenant_id="t")
            )
        assert sf.call_args.kwargs["include_episodes"] is True
