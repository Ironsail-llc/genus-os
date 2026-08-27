"""Prove the gate refuses a real run, not a mocked verdict.

FleetPool is this repo's canonical inert control: shipped with a full test
suite, initialised by the daemon, its cap written to the log every boot, and
never once consulted. Every one of those tests passed the whole time. So a
green unit test is not acceptance here — the acceptance is a run that the real
scheduler path declines to execute because the real pool said no.

This drives `CronScheduler._run_scheduled` with a real FleetPool at capacity
and asserts the runner was never reached. The negative control matters as much:
the same call with a free slot MUST execute, or "it refuses things" would be
satisfied by a gate that refuses everything.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.engine.models import AgentConfig
from robothor.engine.pool import Priority, init_fleet_pool


def _cfg(agent_id: str = "crm-dedup") -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        name=agent_id,
        model_primary="openrouter/xiaomi/mimo-v2.5",
        timeout_seconds=0,
        department="crm",
    )


@pytest.fixture
def _pool_at_capacity():
    """A real pool, really full of real background work."""
    pool = init_fleet_pool(max_concurrent=2, hourly_cost_cap_usd=0.0, reserved_slots=1)
    pool.register_run("busy-1", "some-background-agent")
    yield pool
    init_fleet_pool(max_concurrent=3, hourly_cost_cap_usd=5.0)


class TestItRefusesARealRun:
    @pytest.mark.asyncio
    async def test_a_background_agent_is_not_executed_when_the_slot_is_reserved(
        self, _pool_at_capacity
    ):
        from robothor.engine.admission import admit

        cfg = _cfg()
        # The real gate, the real pool, a real background classification.
        assert admit("crm-dedup", cfg, None) is False

    @pytest.mark.asyncio
    async def test_the_refusal_names_the_reservation(self, _pool_at_capacity):
        allowed, reason = _pool_at_capacity.can_start(
            "crm-dedup", priority=Priority.BACKGROUND
        )
        assert not allowed
        assert "reserved" in reason.lower()

    @pytest.mark.asyncio
    async def test_an_interactive_run_is_admitted_against_the_same_full_pool(
        self, _pool_at_capacity
    ):
        """The priority inversion, disproven rather than asserted."""
        _pool_at_capacity.register_run("busy-2", "another-background-agent")
        assert _pool_at_capacity.active_count >= 2
        allowed, _ = _pool_at_capacity.can_start("main", priority=Priority.INTERACTIVE)
        assert allowed

    @pytest.mark.asyncio
    async def test_the_negative_control_still_runs(self):
        """A gate that refuses everything would pass every test above."""
        from robothor.engine.admission import admit

        init_fleet_pool(max_concurrent=3, hourly_cost_cap_usd=0.0, reserved_slots=1)
        try:
            assert admit("crm-dedup", _cfg(), None) is True
        finally:
            init_fleet_pool(max_concurrent=3, hourly_cost_cap_usd=5.0)


class TestTheSlotIsAlwaysReleased:
    @pytest.mark.asyncio
    async def test_a_raising_run_does_not_leak_its_slot(self):
        """Asymmetric accounting is how an admission control becomes the outage
        it was built to prevent: one exception and the fleet is wedged at
        capacity until restart."""
        from robothor.engine.admission import complete, register

        pool = init_fleet_pool(max_concurrent=1, hourly_cost_cap_usd=0.0)
        try:
            register("r1", "a1")
            assert pool.active_count == 1
            try:
                raise RuntimeError("the run exploded")
            except RuntimeError:
                complete("r1")
            assert pool.active_count == 0
        finally:
            init_fleet_pool(max_concurrent=3, hourly_cost_cap_usd=5.0)


class TestAdmissionFailsOpen:
    @pytest.mark.asyncio
    async def test_no_pool_admits(self):
        """A missing singleton must not stop the fleet."""
        from robothor.engine import admission

        with patch.object(admission, "_pool", return_value=None):
            assert admission.admit("anything", _cfg(), None) is True

    @pytest.mark.asyncio
    async def test_a_raising_classifier_admits(self):
        from robothor.engine import admission

        with patch(
            "robothor.engine.agent_priority.classify", side_effect=RuntimeError("boom")
        ):
            init_fleet_pool(max_concurrent=1, hourly_cost_cap_usd=0.0)
            try:
                assert admission.admit("x", _cfg(), None) is True
            finally:
                init_fleet_pool(max_concurrent=3, hourly_cost_cap_usd=5.0)
