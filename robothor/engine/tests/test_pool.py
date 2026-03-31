"""Tests for the fleet pool manager."""

from __future__ import annotations

import time

from robothor.engine.pool import FleetPool, get_fleet_pool, init_fleet_pool


class TestFleetPool:
    """Tests for FleetPool admission control."""

    def test_can_start_when_empty(self):
        pool = FleetPool(max_concurrent=3, hourly_cost_cap_usd=5.0)
        allowed, reason = pool.can_start("agent-a")
        assert allowed is True
        assert reason == ""

    def test_blocks_at_capacity(self):
        pool = FleetPool(max_concurrent=2, hourly_cost_cap_usd=0)
        pool.register_run("run-1", "agent-a")
        pool.register_run("run-2", "agent-b")
        allowed, reason = pool.can_start("agent-c")
        assert allowed is False
        assert "capacity" in reason

    def test_allows_after_completion(self):
        pool = FleetPool(max_concurrent=1, hourly_cost_cap_usd=0)
        pool.register_run("run-1", "agent-a")
        assert pool.can_start("agent-b")[0] is False
        pool.complete_run("run-1", cost_usd=0.10)
        assert pool.can_start("agent-b")[0] is True

    def test_hourly_cost_cap(self):
        pool = FleetPool(max_concurrent=100, hourly_cost_cap_usd=1.0)
        pool.register_run("run-1", "agent-a")
        pool.complete_run("run-1", cost_usd=0.60)
        pool.register_run("run-2", "agent-b")
        pool.complete_run("run-2", cost_usd=0.50)
        # Total $1.10 > $1.00 cap
        allowed, reason = pool.can_start("agent-c")
        assert allowed is False
        assert "cost cap" in reason.lower()

    def test_hourly_cost_includes_active_runs(self):
        pool = FleetPool(max_concurrent=100, hourly_cost_cap_usd=1.0)
        pool.register_run("run-1", "agent-a")
        pool.update_cost("run-1", 0.90)
        # Active run costs count toward cap
        allowed, reason = pool.can_start("agent-b")
        assert allowed is True  # 0.90 < 1.0
        pool.update_cost("run-1", 1.10)
        allowed, reason = pool.can_start("agent-b")
        assert allowed is False  # 1.10 > 1.0

    def test_cost_cap_zero_means_unlimited(self):
        pool = FleetPool(max_concurrent=100, hourly_cost_cap_usd=0)
        for i in range(10):
            pool.register_run(f"run-{i}", "agent-a")
            pool.complete_run(f"run-{i}", cost_usd=100.0)
        allowed, _ = pool.can_start("agent-a")
        assert allowed is True  # No cost cap

    def test_active_count(self):
        pool = FleetPool(max_concurrent=10)
        assert pool.active_count == 0
        pool.register_run("r1", "a1")
        pool.register_run("r2", "a2")
        assert pool.active_count == 2
        pool.complete_run("r1")
        assert pool.active_count == 1

    def test_stats(self):
        pool = FleetPool(max_concurrent=5, hourly_cost_cap_usd=10.0)
        pool.register_run("r1", "agent-a")
        pool.update_cost("r1", 0.50)
        stats = pool.stats()
        assert stats["active_runs"] == 1
        assert stats["max_concurrent"] == 5
        assert stats["hourly_cost_cap_usd"] == 10.0
        assert len(stats["active_agents"]) == 1
        assert stats["active_agents"][0]["agent_id"] == "agent-a"

    def test_complete_unknown_run_is_noop(self):
        pool = FleetPool(max_concurrent=5)
        pool.complete_run("nonexistent")  # Should not raise
        assert pool.active_count == 0

    def test_cost_history_pruning(self):
        pool = FleetPool(max_concurrent=100, hourly_cost_cap_usd=1.0)
        # Add a cost record with a fake old timestamp
        pool.register_run("old", "a")
        pool.complete_run("old", cost_usd=999.0)
        # Manually age the record
        pool._cost_history[0].completed_at = time.monotonic() - 3700  # >1h ago
        # Should be pruned; new run allowed
        allowed, _ = pool.can_start("agent-b")
        assert allowed is True

    def test_hourly_cost_property(self):
        pool = FleetPool(max_concurrent=100, hourly_cost_cap_usd=10.0)
        pool.register_run("r1", "a")
        pool.complete_run("r1", cost_usd=2.50)
        assert pool.hourly_cost == 2.50


class TestFleetPoolSingleton:
    """Tests for the module-level singleton."""

    def test_init_and_get(self):
        pool = init_fleet_pool(max_concurrent=7, hourly_cost_cap_usd=3.0)
        assert get_fleet_pool() is pool
        assert pool._max_concurrent == 7
        assert pool._hourly_cost_cap_usd == 3.0

    def test_get_before_init_returns_none(self):
        from robothor.engine import pool as pool_module

        old = pool_module._fleet_pool
        try:
            pool_module._fleet_pool = None
            assert get_fleet_pool() is None
        finally:
            pool_module._fleet_pool = old
