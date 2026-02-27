"""Tests for health summary — PostgreSQL version of the original tests."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from robothor.health import dal
from robothor.health.summary import (
    format_duration,
    generate_summary,
    stress_label,
    write_summary,
)


pytestmark = pytest.mark.integration


def _insert_full_data(ref_time: datetime):
    """Insert a complete set of test data keyed to the reference time."""
    today = ref_time.strftime("%Y-%m-%d")
    midnight = ref_time.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_ts = int(midnight.timestamp())

    dal.upsert_sleep([(
        today, midnight_ts - 29460, midnight_ts, 29460, 3960, 19080,
        6420, 960, 83, "GOOD", json.dumps({}),
    )])

    dal.upsert_body_battery([
        (midnight_ts + 3600, 85, 50, 1),
        (midnight_ts + 21600, 61, 50, 1),
    ])

    dal.upsert_stress([
        (midnight_ts, 15),
        (midnight_ts + 1800, 30),
        (midnight_ts + 3600, 45),
        (midnight_ts + 5400, 20),
        (midnight_ts + 7200, 10),
    ])

    dal.upsert_steps([(today, 5806, 7300, 4989.0, 2804, midnight_ts)])

    dal.upsert_resting_heart_rate([(today, 55, midnight_ts)])

    dal.upsert_hrv([(midnight_ts + 7200, 45.0, "reading", None)])


class TestFullData:
    def test_full_data(self):
        ref = datetime(2026, 2, 27, 7, 0, 0)
        _insert_full_data(ref)

        output = generate_summary(now=ref)

        assert "# Health Status" in output
        assert "## Last Night" in output
        assert "## Today" in output

        # Sleep
        assert "Sleep: 8h 11m (score 83, GOOD)" in output
        assert "Deep 1h 06m" in output
        assert "Light 5h 18m" in output
        assert "REM 1h 47m" in output

        # HRV
        assert "HRV: 45 ms" in output

        # Body Battery
        assert "Body Battery: 61 (peak 85)" in output

        # Resting HR
        assert "Resting HR: 55 bpm" in output

        # Stress
        assert "Stress: avg 24 (peak 45) — rest" in output

        # Steps
        assert "Steps: 5,806 / 7,300 (80%)" in output


class TestMissingSleep:
    def test_missing_sleep(self):
        ref = datetime(2026, 2, 27, 7, 0, 0)
        output = generate_summary(now=ref)
        assert "Sleep: N/A" in output

    def test_sleep_fallback_to_yesterday(self):
        ref = datetime(2026, 2, 27, 7, 0, 0)
        dal.upsert_sleep([("2026-02-26", 0, 0, 28800, 3600, 18000,
                           7200, 900, 76, "FAIR", None)])
        output = generate_summary(now=ref)
        assert "Sleep: 8h 00m (score 76, FAIR)" in output


class TestMissingSteps:
    def test_missing_steps(self):
        ref = datetime(2026, 2, 27, 7, 0, 0)
        output = generate_summary(now=ref)
        assert "Steps: N/A" in output


class TestEmptyDB:
    def test_empty_db(self):
        ref = datetime(2026, 2, 27, 7, 0, 0)
        output = generate_summary(now=ref)
        assert "Sleep: N/A" in output
        assert "HRV: N/A" in output
        assert "Body Battery: N/A" in output
        assert "Resting HR: N/A" in output
        assert "Stress: N/A" in output
        assert "Steps: N/A" in output
        assert "# Health Status" in output


class TestStressLabels:
    @pytest.mark.parametrize(
        "avg, expected",
        [
            (0, "rest"),
            (25, "rest"),
            (26, "low"),
            (50, "low"),
            (51, "medium"),
            (75, "medium"),
            (76, "high"),
            (100, "high"),
            (None, "N/A"),
        ],
    )
    def test_stress_labels(self, avg, expected):
        assert stress_label(avg) == expected


class TestDurationFormat:
    @pytest.mark.parametrize(
        "seconds, expected",
        [
            (29460, "8h 11m"),
            (3960, "1h 06m"),
            (3600, "1h 00m"),
            (1800, "30m"),
            (0, "0m"),
            (None, "N/A"),
        ],
    )
    def test_duration_format(self, seconds, expected):
        assert format_duration(seconds) == expected


class TestAtomicWrite:
    def test_atomic_write(self, tmp_path):
        output = tmp_path / "garmin-health.md"
        write_summary("# Test\nHello\n", output_path=output)
        assert output.read_text() == "# Test\nHello\n"

    def test_overwrites_existing(self, tmp_path):
        output = tmp_path / "garmin-health.md"
        output.write_text("old content")
        write_summary("new content\n", output_path=output)
        assert output.read_text() == "new content\n"


class TestOutputSize:
    def test_output_under_1000_chars(self):
        ref = datetime(2026, 2, 27, 7, 0, 0)
        _insert_full_data(ref)
        output = generate_summary(now=ref)
        assert len(output) < 1000, f"Output too long: {len(output)} chars"

    def test_empty_output_under_1000_chars(self):
        ref = datetime(2026, 2, 27, 7, 0, 0)
        output = generate_summary(now=ref)
        assert len(output) < 1000, f"Output too long: {len(output)} chars"
