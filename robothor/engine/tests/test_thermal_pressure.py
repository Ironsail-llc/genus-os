"""Heat is a reason to slow down. It is never a reason to lose work.

2026-08-27. A mature thermal control loop already existed -- ``thermal-guard.sh``
on a 30s timer, four-state hysteresis, a latch against flapping, and real
cooldown stamps from three separate incidents. The gap was never the governor.
It was that ``grep -rn thermal --include=*.py robothor/`` returned only
comments: the scheduler, the pool and the LLM client had no idea the box was
hot, and the guard's only lever was CPU frequency. It could not shed agent work
because nothing in Python could see a temperature.

So this is a read-only peer, not a second governor. It shares the shell guard's
sensors AND its thresholds -- ``test_the_thresholds_match_the_shell_guard``
parses the script and fails if the two ever drift, because two thermal policies
disagreeing is worse than one.

Two invariants matter most. Absent sensors report *unavailable*, never a
comfortable default -- unknown is not "cool". And nothing here can end a run:
the response to heat is fewer concurrent runs, never a cancelled one.
"""

import pathlib
import re

import pytest

import robothor.engine.thermal_pressure as tp


@pytest.fixture
def zones(tmp_path, monkeypatch):
    """A fake /sys/class/thermal with writable zone temperatures."""
    root = tmp_path / "thermal"
    for i in range(3):
        z = root / f"thermal_zone{i}"
        z.mkdir(parents=True)
        (z / "temp").write_text("40000\n")
    monkeypatch.setattr(tp, "_THERMAL_ROOT", str(root))

    def set_max(celsius: float) -> None:
        (root / "thermal_zone1" / "temp").write_text(f"{int(celsius * 1000)}\n")

    return set_max


class TestReadingTheSensors:
    def test_the_hottest_zone_wins(self, zones):
        zones(71.5)
        assert tp.read_max_temperature_c() == pytest.approx(71.5)

    def test_absent_sensors_are_unavailable_not_comfortable(self, monkeypatch):
        """Unknown is not 'cool'. A silently-passing reading here would let a
        laptop with no sensors run flat out into a hard-cut."""
        monkeypatch.setattr(tp, "_THERMAL_ROOT", "/nonexistent")
        assert tp.read_max_temperature_c() is None
        assert tp.thermal_pressure() is None

    def test_garbage_in_a_zone_is_skipped_not_fatal(self, zones, monkeypatch):
        root = pathlib.Path(tp._THERMAL_ROOT)
        (root / "thermal_zone0" / "temp").write_text("not-a-number\n")
        zones(66.0)
        assert tp.read_max_temperature_c() == pytest.approx(66.0)


class TestLevels:
    def test_a_cool_box_is_nominal(self, zones):
        zones(55.0)
        assert tp.thermal_pressure().level is tp.ThermalLevel.NOMINAL

    def test_crossing_the_throttle_point_registers(self, zones):
        zones(tp.THROTTLE_C + 1)
        r = tp.thermal_pressure()
        assert r.level is tp.ThermalLevel.THROTTLE
        assert r.observed_c == pytest.approx(tp.THROTTLE_C + 1)

    def test_warn_and_critical_escalate(self, zones):
        zones(tp.WARN_C + 1)
        assert tp.thermal_pressure().level is tp.ThermalLevel.WARN
        zones(tp.CRIT_C + 1)
        assert tp.thermal_pressure().level is tp.ThermalLevel.CRITICAL


class TestPressureShedsWorkNeverKillsIt:
    def test_rising_temperature_lowers_the_concurrency_budget(self, zones):
        gov = tp.ThermalGovernor()
        zones(50.0)
        cool = gov.concurrency_for(base_slots=4)
        zones(tp.THROTTLE_C + 2)
        hot = gov.concurrency_for(base_slots=4)
        assert hot < cool

    def test_it_never_reduces_below_one_slot(self, zones):
        gov = tp.ThermalGovernor()
        zones(tp.CRIT_C + 5)
        assert gov.concurrency_for(base_slots=4) >= 1
        assert gov.concurrency_for(base_slots=1) >= 1

    def test_unreadable_sensors_leave_the_budget_untouched(self, monkeypatch):
        monkeypatch.setattr(tp, "_THERMAL_ROOT", "/nonexistent")
        assert tp.ThermalGovernor().concurrency_for(base_slots=3) == 3

    def test_nothing_here_can_end_a_run(self):
        """The response to heat is fewer runs, never a cancelled one."""
        source = pathlib.Path(tp.__file__).read_text().lower()
        for banned in ("cancel", "kill", "terminate", "reboot", ".stop("):
            assert banned not in source, f"thermal response must not {banned!r} work"


class TestHysteresis:
    def test_cooling_below_the_throttle_point_is_not_enough_to_restore(self, zones):
        """The shell guard holds its cap until RESTORE_C for a reason: releasing
        at the trip point oscillates."""
        gov = tp.ThermalGovernor()
        zones(tp.THROTTLE_C + 2)
        gov.concurrency_for(base_slots=4)
        zones(tp.THROTTLE_C - 2)  # cooler, but still above RESTORE_C
        assert gov.concurrency_for(base_slots=4) < 4

    def test_cooling_past_the_restore_point_releases(self, zones):
        gov = tp.ThermalGovernor()
        zones(tp.THROTTLE_C + 2)
        gov.concurrency_for(base_slots=4)
        zones(tp.RESTORE_C - 1)
        assert gov.concurrency_for(base_slots=4) == 4


class TestOnePolicyTwoConsumers:
    def test_the_thresholds_match_the_shell_guard(self):
        """Two thermal policies that disagree are worse than one."""
        script = pathlib.Path(tp.__file__).parents[2] / "scripts" / "thermal-guard.sh"
        text = script.read_text()
        for name, value in (
            ("WARN_C", tp.WARN_C),
            ("CRIT_C", tp.CRIT_C),
            ("THROTTLE_C", tp.THROTTLE_C),
            ("RESTORE_C", tp.RESTORE_C),
        ):
            m = re.search(rf"^{name}=\$\{{[A-Z_]+:-(\d+)\}}", text, re.MULTILINE)
            assert m, f"{name} not found in thermal-guard.sh"
            assert int(m.group(1)) == value, f"{name} drifted: shell={m.group(1)} python={value}"

    def test_the_same_env_vars_override_both(self, monkeypatch):
        script = (pathlib.Path(tp.__file__).parents[2] / "scripts" / "thermal-guard.sh").read_text()
        for var in (
            "ROBOTHOR_THERMAL_WARN_C",
            "ROBOTHOR_THERMAL_CRIT_C",
            "ROBOTHOR_THERMAL_THROTTLE_C",
            "ROBOTHOR_THERMAL_RESTORE_C",
        ):
            assert var in script, f"{var} is not the shell guard's knob"
            assert var in pathlib.Path(tp.__file__).read_text(), f"{var} unread by Python"
