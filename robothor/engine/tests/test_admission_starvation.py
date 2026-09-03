"""One inference slot must not starve CRITICAL work.

Measured on a LOCAL episode: the fleet pool is sized from the device's
inference capacity, and a single-GPU box reports ONE slot. `mode_policy`
refuses to reserve at that size — holding the only slot back would stall the
fleet — so `reserved_slots` is 0 and the slot goes to whoever asked first.
Whoever asked first is cron, and main's CRITICAL heartbeat was refused by
admission 12 times in one episode while BACKGROUND sweeps held the slot.

The fix is deliberately NOT to ask admission from more places. Two things
change:

* at a one-slot cap, CRITICAL gets ONE slot of bounded overflow — the
  smallest bound that makes a two-deep queue behind background work
  impossible, while BACKGROUND stays capped at the real limit; and
* sub-agent fan-out is sized from the same mode policy at daemon boot,
  instead of the cloud-shaped default of 10 that `config.py` applies
  regardless of device.

The spawn path must never call `admit()`: a child asking for a slot its
parent is already holding is a deadlock, not a queue.
"""

from __future__ import annotations

from unittest.mock import patch

from robothor.engine.config import EngineConfig
from robothor.engine.pool import FleetPool, Priority


class TestCriticalIsNotStarvedAtOneSlot:
    def test_critical_admitted_over_one_background_run(self) -> None:
        """The reported starvation, in one assertion."""
        pool = FleetPool(max_concurrent=1, hourly_cost_cap_usd=0)
        pool.register_run("bg-1", "crm-sweep")

        allowed, reason = pool.can_start("main", Priority.CRITICAL)

        assert allowed is True, reason

    def test_background_is_still_refused_at_one_slot(self) -> None:
        """The overflow is for CRITICAL only — it is not a cap increase."""
        pool = FleetPool(max_concurrent=1, hourly_cost_cap_usd=0)
        pool.register_run("bg-1", "crm-sweep")

        allowed, reason = pool.can_start("crm-sweep-2", Priority.BACKGROUND)

        assert allowed is False
        assert "capacity" in reason

    def test_the_overflow_is_bounded_at_one(self) -> None:
        """Bounded, not unbounded: a second CRITICAL run does not stack."""
        pool = FleetPool(max_concurrent=1, hourly_cost_cap_usd=0)
        pool.register_run("bg-1", "crm-sweep")
        pool.register_run("crit-1", "main")

        allowed, reason = pool.can_start("main", Priority.CRITICAL)

        assert allowed is False
        assert "capacity" in reason

    def test_multi_slot_pools_are_unchanged(self) -> None:
        """The overflow applies only where reservation is impossible."""
        pool = FleetPool(max_concurrent=2, hourly_cost_cap_usd=0)
        pool.register_run("r1", "a")
        pool.register_run("r2", "b")

        assert pool.can_start("main", Priority.CRITICAL)[0] is False

    def test_the_cost_cap_still_binds_the_overflow(self) -> None:
        """Overflow buys a slot, never a budget."""
        pool = FleetPool(max_concurrent=1, hourly_cost_cap_usd=1.0)
        pool.register_run("bg-1", "crm-sweep")
        pool.update_cost("bg-1", 2.0)

        allowed, reason = pool.can_start("main", Priority.CRITICAL)

        assert allowed is False
        assert "cost cap" in reason.lower()


class TestSpawnFanOutIsSizedFromTheModePolicy:
    """`ROBOTHOR_MAX_CONCURRENT_SPAWNS` defaults to 10 regardless of device."""

    @staticmethod
    def _init(policy_runs: int, configured_spawns: int) -> int:
        from robothor.engine import daemon
        from robothor.engine.mode_policy import ModePolicy

        recorded: list[int] = []

        def _policy(cloud_max_concurrent: int = 3) -> ModePolicy:
            from robothor.engine.execution_mode import ExecutionMode

            return ModePolicy(
                mode=ExecutionMode.LOCAL,
                max_concurrent_runs=policy_runs,
                reserved_interactive_slots=0,
                time_budget_multiplier=1.0,
                monetary_governor=False,
                thermal_governor=False,
                capacity_retries=1,
                request_timeout_seconds=600,
            )

        with (
            patch("robothor.engine.pool.init_fleet_pool"),
            patch("robothor.engine.capacity_governor.CapacityGovernor"),
            patch("robothor.engine.mode_policy.current_policy", _policy),
            patch(
                "robothor.engine.tools.handlers.spawn.set_max_concurrent_spawns",
                recorded.append,
            ),
        ):
            daemon._init_fleet_capacity(
                EngineConfig(max_concurrent_spawns=configured_spawns, max_concurrent_agents=3)
            )
        assert recorded, "boot never sized the spawn fan-out"
        return recorded[-1]

    def test_one_slot_device_bounds_spawns_to_one(self) -> None:
        assert self._init(policy_runs=1, configured_spawns=10) == 1

    def test_the_configured_limit_is_never_widened(self) -> None:
        """A device with headroom does not raise an operator's own ceiling."""
        assert self._init(policy_runs=8, configured_spawns=4) == 4
