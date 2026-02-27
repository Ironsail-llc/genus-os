"""Tests for health sync — mock Garmin API, verify DAL calls."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robothor.health import sync


pytestmark = pytest.mark.integration


def _mock_garmin():
    """Create a mock Garmin client."""
    return MagicMock()


class TestSyncHeartRate:
    def test_basic_sync(self):
        client = _mock_garmin()
        client.get_heart_rates.return_value = {
            "heartRateValues": [
                [1709000000000, 72],  # ms timestamp
                [1709000060000, 75],
            ],
            "restingHeartRate": 55,
            "startTimestampGMT": "2026-02-27T00:00:00.0",
        }
        count = sync.sync_heart_rate(client, "2026-02-27")
        assert count == 2

    def test_no_data(self):
        client = _mock_garmin()
        client.get_heart_rates.return_value = None
        assert sync.sync_heart_rate(client, "2026-02-27") == 0

    def test_empty_values(self):
        client = _mock_garmin()
        client.get_heart_rates.return_value = {"heartRateValues": []}
        assert sync.sync_heart_rate(client, "2026-02-27") == 0

    def test_null_entries_skipped(self):
        client = _mock_garmin()
        client.get_heart_rates.return_value = {
            "heartRateValues": [
                [1709000000000, None],  # null HR
                [1709000060000, 72],
            ],
        }
        count = sync.sync_heart_rate(client, "2026-02-27")
        assert count == 1


class TestSyncStress:
    def test_basic_sync(self):
        client = _mock_garmin()
        client.get_stress_data.return_value = {
            "stressValuesArray": [
                [1709000000, 35],
                [1709000060, 50],
                [1709000120, -1],  # negative → skip
            ],
        }
        assert sync.sync_stress(client, "2026-02-27") == 2


class TestSyncBodyBattery:
    def test_basic_sync(self):
        client = _mock_garmin()
        client.get_body_battery.return_value = [
            {
                "charged": 50,
                "drained": 10,
                "bodyBatteryValuesArray": [
                    [1709000000000, 85],
                    [1709000060000, 82],
                ],
            }
        ]
        assert sync.sync_body_battery(client, "2026-02-27") == 2


class TestSyncSleep:
    def test_basic_sync(self):
        client = _mock_garmin()
        client.get_sleep_data.return_value = {
            "dailySleepDTO": {
                "sleepStartTimestampGMT": 1709000000000,
                "sleepEndTimestampGMT": 1709029460000,
                "sleepTimeSeconds": 29460,
                "deepSleepSeconds": 3960,
                "lightSleepSeconds": 19080,
                "remSleepSeconds": 6420,
                "awakeSleepSeconds": 960,
                "sleepScores": {"overall": {"value": 83, "qualifierKey": "GOOD"}},
            }
        }
        assert sync.sync_sleep(client, "2026-02-27") == 1


class TestSyncHrv:
    def test_summary_and_readings(self):
        client = _mock_garmin()
        client.get_hrv_data.return_value = {
            "hrvSummary": {
                "startTimestampGMT": "2026-02-27T00:00:00.0",
                "weeklyAvg": 42,
                "status": "BALANCED",
            },
            "hrvReadings": [
                {"readingTimeGMT": "2026-02-27T02:30:00.0", "hrvValue": 45, "status": "BALANCED"},
            ],
        }
        assert sync.sync_hrv(client, "2026-02-27") == 2


class TestSyncSteps:
    def test_with_stats(self):
        client = _mock_garmin()
        client.get_steps_data.return_value = [{"steps": 500}]
        client.get_stats.return_value = {
            "totalSteps": 5806,
            "dailyStepGoal": 7300,
            "totalDistanceMeters": 4989.0,
            "totalKilocalories": 2804,
            "calendarDate": "2026-02-27",
            "activeKilocalories": 500,
            "bmrKilocalories": 1800,
            "floorsAscended": 5,
            "intensityMinutesGoal": 150,
        }
        assert sync.sync_steps(client, "2026-02-27") == 1


class TestSyncActivities:
    def test_basic_sync(self):
        client = _mock_garmin()
        client.get_activities.return_value = [
            {
                "activityId": 99999,
                "activityName": "Morning Run",
                "activityType": {"typeKey": "running"},
                "startTimeGMT": "2026-02-27T07:00:00.0",
                "duration": 1800,
                "distance": 5000.0,
                "calories": 300,
                "averageHR": 145,
                "maxHR": 170,
                "averageSpeed": 5.5,
                "elevationGain": 50.0,
                "vO2MaxValue": 42.0,
                "aerobicTrainingEffect": 3.5,
                "anaerobicTrainingEffect": 1.2,
                "activityTrainingLoad": 100,
            }
        ]
        assert sync.sync_activities(client, limit=10) == 1

    def test_no_activities(self):
        client = _mock_garmin()
        client.get_activities.return_value = []
        assert sync.sync_activities(client, limit=10) == 0


class TestSyncDate:
    def test_sync_all_metrics(self):
        """Integration test — sync_date calls all sync functions."""
        client = _mock_garmin()
        # Return empty for everything
        client.get_heart_rates.return_value = None
        client.get_stress_data.return_value = None
        client.get_body_battery.return_value = None
        client.get_sleep_data.return_value = None
        client.get_spo2_data.return_value = None
        client.get_respiration_data.return_value = None
        client.get_hrv_data.return_value = None
        client.get_steps_data.return_value = None
        client.get_training_status.return_value = None

        results = sync.sync_date(client, "2026-02-27")
        assert isinstance(results, dict)
        assert results["heart_rate"] == 0
        assert results["stress"] == 0
