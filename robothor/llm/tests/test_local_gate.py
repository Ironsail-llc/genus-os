"""The local inference gate: what bounds heat, and what must never block.

Measured on the GB10 (docs/runbooks/THERMAL.md): one 27B stream draws ~62W and
plateaus near 85C, a single 7.5k-token request lifts the package 22C in 11s, and
two back-to-back requests cross THROTTLE_C. Recovery is near-instantaneous. So
the gate has to do two things a plain semaphore cannot: bound how many streams
run at once, and *pace* on temperature between them.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.llm import local_gate as lg


@pytest.fixture(autouse=True)
def fresh_gate(monkeypatch):
    """Each test gets its own gate — the real one is a process-wide singleton."""
    monkeypatch.setattr(lg, "_GATE", None)
    yield
    monkeypatch.setattr(lg, "_GATE", None)


@pytest.fixture
def temp(monkeypatch):
    """Drive the temperature the gate sees."""

    def set_c(celsius: float | None) -> None:
        monkeypatch.setattr(lg, "_read_temperature_c", lambda: celsius)

    set_c(50.0)
    return set_c


class TestItBoundsConcurrency:
    async def test_only_slot_count_run_at_once(self, temp):
        gate = lg.LocalInferenceGate(slots=2)
        peak = 0
        live = 0
        lock = asyncio.Lock()

        async def worker():
            nonlocal peak, live
            async with gate.slot(lane=lg.Lane.BACKGROUND):
                async with lock:
                    live += 1
                    peak = max(peak, live)
                await asyncio.sleep(0.05)
                async with lock:
                    live -= 1

        await asyncio.gather(*(worker() for _ in range(8)))
        assert peak == 2, f"gate admitted {peak} concurrent streams against 2 slots"

    async def test_a_slot_is_released_when_the_body_raises(self, temp):
        gate = lg.LocalInferenceGate(slots=1)
        with pytest.raises(ValueError):
            async with gate.slot():
                raise ValueError("boom")
        # If the slot leaked, this would block until the timeout.
        async with gate.slot(timeout=0.5):
            pass

    def test_it_holds_across_separate_event_loops(self, temp):
        """asyncio.Semaphore binds to one loop. rlm_tool and reranker reach this
        through asyncio.run(), which would silently get a fresh, empty gate."""
        gate = lg.LocalInferenceGate(slots=1)
        gate.acquire_sync()
        try:
            with pytest.raises(lg.LocalCapacityBusyError):
                asyncio.run(_take(gate, timeout=0.2))
        finally:
            gate.release()


async def _take(gate, timeout):
    async with gate.slot(timeout=timeout):
        pass


class TestItPacesOnTemperature:
    async def test_background_is_paced_before_the_throttle_point(self, temp):
        gate = lg.LocalInferenceGate(slots=2)
        temp(lg.PACE_BACKGROUND_C + 1)
        with pytest.raises(lg.LocalCapacityBusyError):
            async with gate.slot(lane=lg.Lane.BACKGROUND, timeout=0.2):
                pass

    async def test_interactive_still_gets_through_when_hot(self, temp):
        """A person is waiting. Pacing must never lock the operator out."""
        gate = lg.LocalInferenceGate(slots=2)
        temp(lg.PACE_ALL_C + 2)
        async with gate.slot(lane=lg.Lane.INTERACTIVE, timeout=0.5):
            pass

    async def test_it_admits_again_once_cool(self, temp):
        gate = lg.LocalInferenceGate(slots=2)
        temp(lg.PACE_ALL_C + 2)
        with pytest.raises(lg.LocalCapacityBusyError):
            async with gate.slot(lane=lg.Lane.BACKGROUND, timeout=0.2):
                pass
        temp(lg.RESUME_C - 5)
        async with gate.slot(lane=lg.Lane.BACKGROUND, timeout=0.5):
            pass

    async def test_an_unreadable_sensor_does_not_pace(self, temp):
        """Fail open, matching admission.py. A missing sensor must not stall the
        only tier that answers during a cloud outage."""
        gate = lg.LocalInferenceGate(slots=1)
        temp(None)
        async with gate.slot(lane=lg.Lane.BACKGROUND, timeout=0.5):
            pass


class TestItsRefusalIsUnderstoodDownstream:
    def test_busy_reads_as_a_capacity_error(self):
        from robothor.engine.llm_client import is_capacity_error

        assert is_capacity_error(lg.LocalCapacityBusyError("gate: no slot"))


class TestItResizes:
    async def test_resize_takes_effect(self, temp):
        gate = lg.LocalInferenceGate(slots=1)
        gate.resize(2)
        a = await gate.slot(timeout=0.5).__aenter__()  # noqa: F841
        async with gate.slot(timeout=0.5):
            pass

    def test_resize_to_zero_is_refused(self, temp):
        """A gate of zero is a stalled fleet, not a safe one."""
        gate = lg.LocalInferenceGate(slots=2)
        gate.resize(0)
        assert gate.snapshot()["slots"] >= 1
