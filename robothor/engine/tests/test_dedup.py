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


class TestDedupHA:
    """HA-mode lease renew + safe release (Wave-2 review fixes)."""

    def setup_method(self):
        from robothor.engine import dedup

        dedup.clear()

    def teardown_method(self):
        from robothor.engine import dedup

        dedup.clear()

    @pytest.mark.asyncio
    async def test_acquire_starts_renew_task_release_cancels(self, monkeypatch):
        from robothor.engine import dedup, redis_lease

        monkeypatch.setenv("ROBOTHOR_HA_DEDUP_ENABLED", "1")
        monkeypatch.setattr(redis_lease, "acquire", lambda *a, **k: "tok-A")
        monkeypatch.setattr(redis_lease, "release", lambda *a, **k: True)

        assert await dedup.try_acquire("agent-1") is True
        # A renew loop keeps a >TTL run's lease alive (the missing-renew bug).
        assert "agent-1" in dedup._renew_tasks
        assert dedup._owners["agent-1"] == "tok-A"

        await dedup.release("agent-1")
        assert "agent-1" not in dedup._renew_tasks  # renew stopped
        assert "agent-1" not in dedup._owners  # lease forgotten on success

    @pytest.mark.asyncio
    async def test_release_failure_retains_owner(self, monkeypatch):
        """If the Redis release fails, we must NOT drop the owner token — else
        nothing retries and the 2h-TTL key strands the agent fleet-wide."""
        from robothor.engine import dedup, redis_lease

        monkeypatch.setenv("ROBOTHOR_HA_DEDUP_ENABLED", "1")
        monkeypatch.setattr(redis_lease, "acquire", lambda *a, **k: "tok-A")

        def _boom(*a, **k):
            raise RuntimeError("redis down")

        monkeypatch.setattr(redis_lease, "release", _boom)

        assert await dedup.try_acquire("agent-1") is True
        await dedup.release("agent-1")  # release fails internally, must not raise
        # Token retained so a later release/TTL can still clean it up.
        assert dedup._owners.get("agent-1") == "tok-A"
        assert dedup._renew_tasks.get("agent-1") is None  # renew still stopped
