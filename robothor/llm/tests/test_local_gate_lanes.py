"""With execution lanes on, the local inference gate keeps room for a person.

A burst of background embeddings and summaries could otherwise hold every local
slot when a chat turn asks for one. Under ``ROBOTHOR_EXECUTION_LANES`` the gate
reserves one slot for interactive requests whenever it has two or more, and a
waiting interactive request is served before waiting background work.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from robothor.llm import local_gate as lg


@pytest.fixture(autouse=True)
def cool(monkeypatch):
    monkeypatch.setattr(lg, "_GATE", None)
    monkeypatch.setattr(lg, "_read_temperature_c", lambda: 50.0)
    yield
    monkeypatch.setattr(lg, "_GATE", None)


@pytest.fixture
def lanes_on(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_EXECUTION_LANES", "1")
    monkeypatch.delenv("ROBOTHOR_DISABLE_ALL_RIPS", raising=False)


@pytest.fixture
def lanes_off(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_EXECUTION_LANES", raising=False)


class TestReservedInteractiveSlot:
    async def test_background_cannot_take_the_last_slot(self, lanes_on):
        gate = lg.LocalInferenceGate(slots=2)
        async with gate.slot(lane=lg.Lane.BACKGROUND):
            with pytest.raises(lg.LocalCapacityBusyError):
                async with gate.slot(lane=lg.Lane.BACKGROUND, timeout=0.1):
                    pass

    async def test_interactive_gets_the_reserved_slot_at_once(self, lanes_on):
        gate = lg.LocalInferenceGate(slots=2)
        async with gate.slot(lane=lg.Lane.BACKGROUND):
            t0 = time.perf_counter()
            async with gate.slot(lane=lg.Lane.INTERACTIVE, timeout=1.0):
                waited = time.perf_counter() - t0
        assert waited < 0.05

    async def test_a_normal_request_inside_an_interactive_turn_counts_as_interactive(
        self, lanes_on
    ):
        from robothor.engine.lanes import Lane, in_lane

        gate = lg.LocalInferenceGate(slots=2)
        async with gate.slot(lane=lg.Lane.NORMAL):
            with in_lane(Lane.INTERACTIVE):
                async with gate.slot(lane=lg.Lane.NORMAL, timeout=0.5):
                    pass

    async def test_flag_off_both_background_requests_get_a_slot(self, lanes_off):
        gate = lg.LocalInferenceGate(slots=2)
        async with gate.slot(lane=lg.Lane.BACKGROUND):
            async with gate.slot(lane=lg.Lane.BACKGROUND, timeout=0.1):
                assert gate.snapshot()["active"] == 2

    async def test_a_single_slot_is_not_reserved(self, lanes_on):
        """Holding back the only slot would stall all background work."""
        gate = lg.LocalInferenceGate(slots=1)
        async with gate.slot(lane=lg.Lane.BACKGROUND, timeout=0.1):
            pass


class TestInteractiveGoesFirst:
    async def test_a_waiting_interactive_request_is_served_before_background(self, lanes_on):
        """Five rounds, so first-come polling cannot pass this by luck."""
        for _ in range(5):
            gate = lg.LocalInferenceGate(slots=1)
            order: list[str] = []
            await gate._acquire_async(lg.Lane.BACKGROUND, None)

            async def ask(lane: lg.Lane, label: str, gate=gate, order=order) -> None:
                async with gate.slot(lane=lane, timeout=2.0):
                    order.append(label)
                    await asyncio.sleep(0.03)

            bg = asyncio.create_task(ask(lg.Lane.BACKGROUND, "background"))
            await asyncio.sleep(0.04)
            fg = asyncio.create_task(ask(lg.Lane.INTERACTIVE, "interactive"))
            await asyncio.sleep(0.04)
            gate.release()
            await asyncio.gather(bg, fg)
            assert order == ["interactive", "background"]

    def test_the_snapshot_reports_interactive_waiters(self, lanes_on):
        assert "interactive_waiting" in lg.LocalInferenceGate(slots=2).snapshot()
