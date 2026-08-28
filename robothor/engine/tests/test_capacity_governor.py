"""The wire between what the device can do and what the fleet is allowed to do.

`FleetPool.set_limits`, `ModePolicy` and `ThermalGovernor` all shipped complete and
none of them was ever connected to anything: the pool was sized once at boot from a
cloud-shaped constant and never retuned, while the machine it ran on fell back to a
single local GPU. This is that connection.
"""

from __future__ import annotations

import pytest

from robothor.engine import capacity_governor as cg
from robothor.engine.execution_mode import ExecutionMode


class FakePool:
    def __init__(self):
        self.limits = None

    def set_limits(self, max_concurrent=None, reserved_slots=None):
        self.limits = (max_concurrent, reserved_slots)


@pytest.fixture
def pool(monkeypatch):
    p = FakePool()
    monkeypatch.setattr(cg, "_pool", lambda: p)
    return p


@pytest.fixture
def mode(monkeypatch):
    def set_mode(m):
        monkeypatch.setattr(cg, "_current_mode", lambda: m)

    set_mode(ExecutionMode.LOCAL)
    return set_mode


@pytest.fixture
def temp(monkeypatch):
    def set_c(c):
        monkeypatch.setattr(
            "robothor.engine.thermal_pressure.read_max_temperature_c", lambda: c
        )

    set_c(50.0)
    return set_c


class TestItAppliesTheDevicesCapacity:
    def test_local_sizes_the_pool_from_inference_slots(self, pool, mode, temp, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_LOCAL_MAX_CONCURRENT", "1")
        gov = cg.CapacityGovernor()
        gov.apply_once()
        assert pool.limits is not None
        assert pool.limits[0] == 1, "local pool must be sized by the device, not a constant"

    def test_cloud_keeps_the_configured_fan_out(self, pool, mode, temp):
        mode(ExecutionMode.CLOUD)
        gov = cg.CapacityGovernor(cloud_max_concurrent=3)
        gov.apply_once()
        assert pool.limits[0] == 3, "cloud must not be throttled by this box"

    def test_heat_reduces_local_slots(self, pool, mode, temp, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_LOCAL_MAX_CONCURRENT", "4")
        gov = cg.CapacityGovernor()
        gov.apply_once()
        cool = pool.limits[0]
        temp(91.0)
        gov.apply_once()
        assert pool.limits[0] < cool, "a hot box must admit fewer runs"

    def test_a_cool_box_leaves_cloud_constants_alone(self, pool, mode, temp):
        mode(ExecutionMode.CLOUD)
        temp(50.0)
        cg.CapacityGovernor(cloud_max_concurrent=3).apply_once()
        assert pool.limits[0] == 3, "a cool box must not throttle cloud fan-out"

    def test_heat_derates_even_in_cloud_mode(self, pool, mode, temp):
        """Heat is physical; the mode signal lags reality.

        ModePolicy makes the thermal governor inert in CLOUD, reasoning that
        someone else's datacentre is not our heat budget. True only while the work
        is actually remote. Measured 2026-08-28: with the credential capped every
        agent fell through to the local 27B while the tracker still read
        `mode=cloud runs=3` — it needs three consecutive LOCAL completions plus
        dwell to flip, and the box passed 90C first. A thermometer does not lag.
        """
        mode(ExecutionMode.CLOUD)
        temp(93.0)
        cg.CapacityGovernor(cloud_max_concurrent=3).apply_once()
        assert pool.limits[0] < 3, "a hot box must shed work whatever mode it thinks it is in"

    def test_it_never_admits_zero(self, pool, mode, temp, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_LOCAL_MAX_CONCURRENT", "1")
        temp(99.0)
        gov = cg.CapacityGovernor()
        gov.apply_once()
        assert pool.limits[0] >= 1, "a fleet that admits nothing is stalled, not safe"


class TestItFailsOpen:
    def test_no_pool_is_not_an_error(self, mode, temp, monkeypatch):
        monkeypatch.setattr(cg, "_pool", lambda: None)
        cg.CapacityGovernor().apply_once()  # must not raise

    def test_a_broken_policy_does_not_propagate(self, pool, mode, temp, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("sensor exploded")

        monkeypatch.setattr(cg, "_current_mode", boom)
        cg.CapacityGovernor().apply_once()  # must not raise


class TestItAlsoSizesTheRequestGate:
    def test_the_local_gate_follows_the_same_policy(self, pool, mode, temp, monkeypatch):
        from robothor.llm import local_gate as lg

        monkeypatch.setattr(lg, "_GATE", lg.LocalInferenceGate(slots=8))
        monkeypatch.setenv("ROBOTHOR_LOCAL_MAX_CONCURRENT", "1")
        cg.CapacityGovernor().apply_once()
        assert lg.gate().snapshot()["slots"] == 1
