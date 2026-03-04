#!/usr/bin/env python3
"""
Tests for cron_health_check.py.
"""

import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from cron_health_check import check_jobs, classify_schedule, main, write_status


def _make_job(
    name,
    expr="0 6-22/2 * * *",
    enabled=True,
    last_run_ms=None,
    run_status="ok",
    consecutive_errors=0,
    last_error="",
    delivery_status="delivered",
    duration_ms=50000,
):
    """Helper to build a job dict matching jobs.json structure."""
    if last_run_ms is None:
        last_run_ms = time.time() * 1000 - 600_000  # 10 min ago
    return {
        "id": f"{name}-0001",
        "name": name,
        "enabled": enabled,
        "schedule": {"kind": "cron", "expr": expr, "tz": "America/New_York"},
        "state": {
            "lastRunAtMs": last_run_ms,
            "lastRunStatus": run_status,
            "lastDeliveryStatus": delivery_status,
            "lastDurationMs": duration_ms,
            "consecutiveErrors": consecutive_errors,
            "lastError": last_error,
        },
    }


class TestCheckJobs:
    """Test job classification logic."""

    def test_all_healthy(self):
        jobs = [
            _make_job("Email Classifier"),
            _make_job("Calendar Monitor"),
            _make_job("Supervisor Heartbeat"),
        ]
        result = check_jobs(jobs)
        assert len(result["errors"]) == 0
        assert len(result["stale"]) == 0
        assert len(result["healthy"]) == 3

    def test_error_detection(self):
        jobs = [
            _make_job(
                "Email Classifier",
                run_status="error",
                consecutive_errors=2,
                last_error="Provider returned 400",
            ),
            _make_job("Calendar Monitor"),
        ]
        result = check_jobs(jobs)
        assert len(result["errors"]) == 1
        assert result["errors"][0]["name"] == "Email Classifier"
        assert result["errors"][0]["consecutive_errors"] == 2
        assert len(result["healthy"]) == 1

    def test_stale_detection(self):
        # 2h-interval job last ran 4 hours ago (threshold 2.5h)
        old_ms = time.time() * 1000 - 4 * 3600 * 1000
        jobs = [
            _make_job("Email Classifier", last_run_ms=old_ms),
            _make_job("Calendar Monitor"),
        ]
        result = check_jobs(jobs)
        assert len(result["stale"]) == 1
        assert result["stale"][0]["name"] == "Email Classifier"
        assert len(result["healthy"]) == 1

    def test_disabled_exclusion(self):
        jobs = [
            _make_job("Email Classifier"),
            _make_job("Retired Job", enabled=False),
        ]
        result = check_jobs(jobs)
        assert len(result["healthy"]) == 1
        assert len(result["errors"]) == 0
        assert len(result["stale"]) == 0

    def test_delivery_status_display(self, tmp_path):
        jobs = [
            _make_job("Email Classifier", delivery_status="delivered"),
            _make_job("Vision Monitor", delivery_status="not-delivered"),
        ]
        result = check_jobs(jobs)
        output_path = tmp_path / "cron-health-status.md"
        write_status(result, output_path)
        content = output_path.read_text()
        assert "[delivered]" in content
        assert "[silent]" in content

    def test_missing_jobs_json(self, tmp_path):
        """Gracefully handle missing jobs.json."""
        fake_jobs = tmp_path / "nonexistent" / "jobs.json"
        output_path = tmp_path / "cron-health-status.md"

        with (
            patch("cron_health_check.JOBS_JSON_PATH", fake_jobs),
            patch("cron_health_check.OUTPUT_PATH", output_path),
        ):
            main()

        content = output_path.read_text()
        assert "All 0 agents healthy" in content


class TestClassifySchedule:
    """Test schedule expression classification."""

    def test_every_2h(self):
        assert classify_schedule("0 6-22/2 * * *") == "2h"

    def test_every_4h(self):
        assert classify_schedule("0 8-20/4 * * *") == "4h"

    def test_hourly(self):
        assert classify_schedule("0 * * * *") == "hourly"

    def test_hourly_range(self):
        assert classify_schedule("0 6-22 * * *") == "hourly"

    def test_daily(self):
        assert classify_schedule("30 6 * * *") == "daily"

    def test_3x_daily(self):
        assert classify_schedule("0 8,14,20 * * *") == "3x_daily"
