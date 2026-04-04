"""Tests for the dedup module — cross-trigger agent deduplication."""

import pytest

from robothor.engine.dedup import (
    clear,
    is_running,
    release,
    release_sync,
    running_agents,
    try_acquire,
)


class TestDedup:
    def setup_method(self):
        clear()

    def teardown_method(self):
        clear()

    @pytest.mark.asyncio
    async def test_acquire_succeeds(self):
        assert await try_acquire("agent-1") is True
        assert is_running("agent-1") is True

    @pytest.mark.asyncio
    async def test_duplicate_blocked(self):
        assert await try_acquire("agent-1") is True
        assert await try_acquire("agent-1") is False

    @pytest.mark.asyncio
    async def test_release_allows_reacquire(self):
        assert await try_acquire("agent-1") is True
        await release("agent-1")
        assert is_running("agent-1") is False
        assert await try_acquire("agent-1") is True

    @pytest.mark.asyncio
    async def test_running_agents_returns_copy(self):
        await try_acquire("a")
        await try_acquire("b")
        agents = running_agents()
        assert agents == {"a", "b"}
        # Modifying the copy doesn't affect the original
        agents.add("c")
        assert "c" not in running_agents()

    @pytest.mark.asyncio
    async def test_release_nonexistent_no_error(self):
        await release("nonexistent")  # should not raise

    @pytest.mark.asyncio
    async def test_multiple_agents_independent(self):
        assert await try_acquire("agent-1") is True
        assert await try_acquire("agent-2") is True
        assert is_running("agent-1") is True
        assert is_running("agent-2") is True
        await release("agent-1")
        assert is_running("agent-1") is False
        assert is_running("agent-2") is True

    def test_release_sync(self):
        """Sync release for non-async contexts (daemon stale run cleanup)."""
        # Directly add to _running set for testing
        from robothor.engine.dedup import _running

        _running.add("agent-1")
        assert is_running("agent-1") is True
        release_sync("agent-1")
        assert is_running("agent-1") is False
