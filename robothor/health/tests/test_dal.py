"""Tests for health DAL — upsert and query functions."""

from __future__ import annotations

import json

import pytest

from robothor.health import dal


pytestmark = pytest.mark.integration


class TestParseTimestamp:
    def test_none(self):
        assert dal.parse_timestamp(None) is None

    def test_unix_seconds(self):
        assert dal.parse_timestamp(1709000000) == 1709000000

    def test_unix_milliseconds(self):
        assert dal.parse_timestamp(1709000000000) == 1709000000

    def test_float(self):
        assert dal.parse_timestamp(1709000000.5) == 1709000000

    def test_iso_string(self):
        ts = dal.parse_timestamp("2026-02-27T07:00:00")
        assert isinstance(ts, int)
        assert ts > 0

    def test_date_string(self):
        ts = dal.parse_timestamp("2026-02-27")
        assert isinstance(ts, int)

    def test_invalid_string(self):
        assert dal.parse_timestamp("not-a-date") is None


class TestUpsertHeartRate:
    def test_basic_insert(self):
        rows = [(1000001, 72, "monitoring"), (1000002, 75, "monitoring")]
        assert dal.upsert_heart_rate(rows) == 2

    def test_empty(self):
        assert dal.upsert_heart_rate([]) == 0

    def test_upsert_overwrites(self):
        dal.upsert_heart_rate([(1000001, 72, "monitoring")])
        dal.upsert_heart_rate([(1000001, 80, "monitoring")])
        # Should not raise, and second value should win
        from robothor.db.connection import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT heart_rate FROM health_heart_rate WHERE timestamp = 1000001")
                assert cur.fetchone()[0] == 80


class TestUpsertStress:
    def test_basic_insert(self):
        rows = [(2000001, 35), (2000002, 50)]
        assert dal.upsert_stress(rows) == 2


class TestUpsertBodyBattery:
    def test_basic_insert(self):
        rows = [(3000001, 85, 50, 10), (3000002, 61, 50, 10)]
        assert dal.upsert_body_battery(rows) == 2


class TestUpsertSleep:
    def test_basic_insert(self):
        row = ("2026-02-27", 1000, 2000, 29460, 3960, 19080, 6420, 960,
               83, "GOOD", json.dumps({"test": True}))
        assert dal.upsert_sleep([row]) == 1


class TestUpsertHrv:
    def test_basic_insert(self):
        rows = [(4000001, 45.0, "reading", None)]
        assert dal.upsert_hrv(rows) == 1


class TestUpsertSteps:
    def test_basic_insert(self):
        rows = [("2026-02-27", 5806, 7300, 4989.0, 2804, 1000)]
        assert dal.upsert_steps(rows) == 1


class TestUpsertActivities:
    def test_basic_insert(self):
        rows = [(99999, "Morning Run", "running", 5000001, 1800,
                 5000.0, 300, 145, 170, 5.5, 50.0, 42.0, 3.5, 1.2, 100,
                 json.dumps({"test": True}))]
        assert dal.upsert_activities(rows) == 1

    def test_empty(self):
        assert dal.upsert_activities([]) == 0


class TestLogSync:
    def test_log_success(self):
        dal.log_sync("heart_rate", 100, "success")
        from robothor.db.connection import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT metric_type, records_synced FROM health_sync_log")
                row = cur.fetchone()
                assert row[0] == "heart_rate"
                assert row[1] == 100

    def test_log_error(self):
        dal.log_sync("stress", 0, "error", "API timeout")
        from robothor.db.connection import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT error_message FROM health_sync_log")
                assert cur.fetchone()[0] == "API timeout"


class TestGetSleep:
    def test_returns_today(self):
        dal.upsert_sleep([("2026-02-27", 1000, 2000, 29460, 3960, 19080,
                           6420, 960, 83, "GOOD", None)])
        result = dal.get_sleep("2026-02-27", "2026-02-26")
        assert result["total"] == 29460
        assert result["score"] == 83

    def test_falls_back_to_yesterday(self):
        dal.upsert_sleep([("2026-02-26", 1000, 2000, 28800, 3600, 18000,
                           7200, 900, 76, "FAIR", None)])
        result = dal.get_sleep("2026-02-27", "2026-02-26")
        assert result["total"] == 28800
        assert result["quality"] == "FAIR"

    def test_empty(self):
        result = dal.get_sleep("2026-02-27", "2026-02-26")
        assert result == {}


class TestGetBodyBattery:
    def test_current_and_peak(self):
        dal.upsert_body_battery([
            (1000, 85, 50, 1),
            (2000, 61, 50, 1),
        ])
        result = dal.get_body_battery(0, 3000)
        assert result["current"] == 61  # latest
        assert result["peak"] == 85

    def test_empty(self):
        result = dal.get_body_battery(0, 3000)
        assert result["current"] is None
        assert result["peak"] is None


class TestGetStressAvg:
    def test_avg_and_peak(self):
        dal.upsert_stress([(100, 15), (200, 30), (300, 45), (400, 20), (500, 10)])
        result = dal.get_stress_avg(0, 1000)
        assert result["avg"] == 24  # round(24.0)
        assert result["peak"] == 45

    def test_empty(self):
        result = dal.get_stress_avg(0, 1000)
        assert result["avg"] is None


class TestGetSteps:
    def test_with_goal(self):
        dal.upsert_steps([("2026-02-27", 5806, 7300, 4989.0, 2804, 1000)])
        result = dal.get_steps("2026-02-27")
        assert result["total"] == 5806
        assert result["goal"] == 7300
        assert result["pct"] == 80

    def test_empty(self):
        result = dal.get_steps("2026-02-27")
        assert result == {}


class TestGetRestingHr:
    def test_today(self):
        dal.upsert_resting_heart_rate([("2026-02-27", 55, 1000)])
        assert dal.get_resting_hr("2026-02-27", "2026-02-26") == 55

    def test_fallback_yesterday(self):
        dal.upsert_resting_heart_rate([("2026-02-26", 58, 1000)])
        assert dal.get_resting_hr("2026-02-27", "2026-02-26") == 58

    def test_none(self):
        assert dal.get_resting_hr("2026-02-27", "2026-02-26") is None


class TestGetHrvLatest:
    def test_latest_reading(self):
        dal.upsert_hrv([
            (1000, 42.0, "reading", None),
            (2000, 45.0, "reading", None),
            (3000, 50.0, "weekly_avg", None),  # Should be excluded
        ])
        result = dal.get_hrv_latest(0, 4000)
        assert result == 45  # latest 'reading' type

    def test_none(self):
        assert dal.get_hrv_latest(0, 4000) is None
