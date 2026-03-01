"""
Sync data from Garmin Connect to PostgreSQL.

Usage:
    python -m robothor.health.sync              # Sync today
    python -m robothor.health.sync --days 3     # Sync last 3 days
    python -m robothor.health.sync --login EMAIL PASSWORD  # Re-authenticate
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from garminconnect import Garmin
from garth.exc import GarthHTTPError

from robothor.health import dal

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_DIR = Path(
    os.environ.get(
        "GARMIN_TOKEN_DIR",
        Path.home() / ".config" / "robothor" / "garmin_tokens",
    )
)


def get_mfa_code() -> str:
    """Prompt user for MFA code."""
    return input("Enter MFA code from your authenticator app: ")


def get_client(
    email: str | None = None,
    password: str | None = None,
    prompt_mfa: bool = False,
    token_dir: Path = DEFAULT_TOKEN_DIR,
) -> Garmin:
    """Get authenticated Garmin client, using cached tokens if available."""
    token_dir.mkdir(parents=True, exist_ok=True)

    try:
        client = Garmin()
        client.garth.load(token_dir)
        client.display_name = client.garth.profile["displayName"]
        client.full_name = client.garth.profile["fullName"]
        print(f"Authenticated using cached tokens (user: {client.display_name})")
        return client
    except (FileNotFoundError, GarthHTTPError, KeyError, Exception) as e:
        if not prompt_mfa:
            print(f"Token auth failed: {e}")
            print("Run with --login to re-authenticate with MFA")
            sys.exit(1)

    if not email:
        email = os.environ.get("GARMIN_EMAIL")
    if not password:
        password = os.environ.get("GARMIN_PASSWORD")

    if not email or not password:
        print("Error: Garmin credentials required for initial login.")
        print("Set GARMIN_EMAIL and GARMIN_PASSWORD environment variables,")
        print("or run: python -m robothor.health.sync --login EMAIL PASSWORD")
        sys.exit(1)

    client = Garmin(email, password, prompt_mfa=get_mfa_code)
    client.login()
    client.garth.dump(token_dir)
    print("Authenticated and tokens cached")
    return client


# ---------------------------------------------------------------------------
# Per-metric sync functions
# ---------------------------------------------------------------------------


def sync_heart_rate(client: Garmin, target_date: str) -> int:
    """Sync heart rate data for a specific date."""
    try:
        data = client.get_heart_rates(target_date)
        if not data or "heartRateValues" not in data:
            return 0

        rows = []
        for entry in data.get("heartRateValues") or []:
            if entry and len(entry) >= 2 and entry[1] is not None:
                ts = entry[0] // 1000 if entry[0] > 10_000_000_000 else entry[0]
                rows.append((ts, entry[1], "monitoring"))

        count = dal.upsert_heart_rate(rows)

        # Resting HR
        rhr = data.get("restingHeartRate")
        if rhr:
            ts = dal.parse_timestamp(data.get("startTimestampGMT"))
            dal.upsert_resting_heart_rate([(target_date, rhr, ts)])

        return count
    except GarthHTTPError as e:
        print(f"  Error fetching heart rate: {e}")
        return 0


def sync_stress(client: Garmin, target_date: str) -> int:
    """Sync stress data for a specific date."""
    try:
        data = client.get_stress_data(target_date)
        if not data or "stressValuesArray" not in data:
            return 0

        rows = []
        for entry in data.get("stressValuesArray", []):
            if entry and len(entry) >= 2 and entry[1] is not None and entry[1] >= 0:
                ts = entry[0] // 1000 if entry[0] > 10_000_000_000 else entry[0]
                rows.append((ts, entry[1]))

        return dal.upsert_stress(rows)
    except GarthHTTPError as e:
        print(f"  Error fetching stress: {e}")
        return 0


def sync_body_battery(client: Garmin, target_date: str) -> int:
    """Sync body battery data for a specific date."""
    try:
        data = client.get_body_battery(target_date)
        if not data:
            return 0

        rows = []
        for day_data in data:
            charged = day_data.get("charged")
            drained = day_data.get("drained")
            for entry in day_data.get("bodyBatteryValuesArray") or []:
                if entry and len(entry) >= 2 and entry[1] is not None:
                    ts = entry[0] // 1000 if entry[0] > 10_000_000_000 else entry[0]
                    rows.append((ts, entry[1], charged, drained))

        return dal.upsert_body_battery(rows)
    except GarthHTTPError as e:
        print(f"  Error fetching body battery: {e}")
        return 0


def sync_sleep(client: Garmin, target_date: str) -> int:
    """Sync sleep data for a specific date."""
    try:
        data = client.get_sleep_data(target_date)
        if not data or "dailySleepDTO" not in data:
            return 0

        sleep = data["dailySleepDTO"]
        row = (
            target_date,
            dal.parse_timestamp(sleep.get("sleepStartTimestampGMT")),
            dal.parse_timestamp(sleep.get("sleepEndTimestampGMT")),
            sleep.get("sleepTimeSeconds"),
            sleep.get("deepSleepSeconds"),
            sleep.get("lightSleepSeconds"),
            sleep.get("remSleepSeconds"),
            sleep.get("awakeSleepSeconds"),
            (
                sleep.get("sleepScores", {}).get("overall", {}).get("value")
                if sleep.get("sleepScores")
                else None
            ),
            (
                sleep.get("sleepScores", {}).get("overall", {}).get("qualifierKey")
                if sleep.get("sleepScores")
                else None
            ),
            json.dumps(data),
        )
        return dal.upsert_sleep([row])
    except GarthHTTPError as e:
        print(f"  Error fetching sleep: {e}")
        return 0


def sync_spo2(client: Garmin, target_date: str) -> int:
    """Sync SpO2 data for a specific date."""
    try:
        data = client.get_spo2_data(target_date)
        if not data:
            return 0

        readings = data if isinstance(data, list) else data.get("spO2Values", [])
        rows = []
        for entry in readings:
            if isinstance(entry, dict):
                ts = dal.parse_timestamp(
                    entry.get("startTimestampGMT") or entry.get("timestampGMT")
                )
                value = entry.get("spO2Value") or entry.get("averageSpO2")
            elif isinstance(entry, list) and len(entry) >= 2:
                ts = entry[0] // 1000 if entry[0] > 10_000_000_000 else entry[0]
                value = entry[1]
            else:
                continue
            if ts and value:
                rows.append((ts, value, "monitoring"))

        return dal.upsert_spo2(rows)
    except GarthHTTPError as e:
        print(f"  Error fetching SpO2: {e}")
        return 0


def sync_respiration(client: Garmin, target_date: str) -> int:
    """Sync respiration data for a specific date."""
    try:
        data = client.get_respiration_data(target_date)
        if not data:
            return 0

        readings = data if isinstance(data, list) else (data.get("respirationValuesArray") or [])
        rows = []
        for entry in readings:
            if isinstance(entry, dict):
                ts = dal.parse_timestamp(entry.get("startTimestampGMT"))
                value = entry.get("respirationValue")
            elif isinstance(entry, list) and len(entry) >= 2:
                ts = entry[0] // 1000 if entry[0] > 10_000_000_000 else entry[0]
                value = entry[1]
            else:
                continue
            if ts and value and value > 0:
                rows.append((ts, value))

        return dal.upsert_respiration(rows)
    except GarthHTTPError as e:
        print(f"  Error fetching respiration: {e}")
        return 0


def sync_hrv(client: Garmin, target_date: str) -> int:
    """Sync HRV data for a specific date."""
    try:
        data = client.get_hrv_data(target_date)
        if not data:
            return 0

        rows = []
        if "hrvSummary" in data:
            summary = data["hrvSummary"]
            ts = dal.parse_timestamp(summary.get("startTimestampGMT"))
            if ts:
                rows.append((ts, summary.get("weeklyAvg"), "weekly_avg", summary.get("status")))

        for entry in data.get("hrvReadings", []):
            ts = dal.parse_timestamp(entry.get("readingTimeGMT"))
            if ts and entry.get("hrvValue"):
                rows.append((ts, entry.get("hrvValue"), "reading", entry.get("status")))

        return dal.upsert_hrv(rows)
    except GarthHTTPError as e:
        print(f"  Error fetching HRV: {e}")
        return 0


def sync_steps(client: Garmin, target_date: str) -> int:
    """Sync steps data for a specific date."""
    try:
        data = client.get_steps_data(target_date)
        if not data:
            return 0

        total = 0
        for entry in data:
            total += entry.get("steps", 0) if isinstance(entry, dict) else 0

        stats = client.get_stats(target_date)
        if stats:
            dal.upsert_steps(
                [
                    (
                        target_date,
                        stats.get("totalSteps", total),
                        stats.get("dailyStepGoal"),
                        stats.get("totalDistanceMeters"),
                        stats.get("totalKilocalories"),
                        dal.parse_timestamp(stats.get("calendarDate")),
                    )
                ]
            )

            dal.upsert_daily_summary(
                [
                    (
                        target_date,
                        stats.get("totalKilocalories"),
                        stats.get("activeKilocalories"),
                        stats.get("bmrKilocalories"),
                        stats.get("floorsAscended"),
                        stats.get("intensityMinutesGoal"),
                        json.dumps(stats),
                    )
                ]
            )

            return 1
    except GarthHTTPError as e:
        print(f"  Error fetching steps: {e}")
    return 0


def sync_activities(client: Garmin, limit: int = 20) -> int:
    """Sync recent activities."""
    try:
        activities = client.get_activities(0, limit)
        if not activities:
            return 0

        rows = []
        for act in activities:
            start_ts = dal.parse_timestamp(act.get("startTimeGMT") or act.get("startTimeLocal"))
            if not start_ts:
                continue
            rows.append(
                (
                    act.get("activityId"),
                    act.get("activityName"),
                    (
                        act.get("activityType", {}).get("typeKey")
                        if isinstance(act.get("activityType"), dict)
                        else act.get("activityType")
                    ),
                    start_ts,
                    act.get("duration"),
                    act.get("distance"),
                    act.get("calories"),
                    act.get("averageHR"),
                    act.get("maxHR"),
                    act.get("averageSpeed"),
                    act.get("elevationGain"),
                    act.get("vO2MaxValue"),
                    act.get("aerobicTrainingEffect"),
                    act.get("anaerobicTrainingEffect"),
                    act.get("activityTrainingLoad"),
                    json.dumps(act),
                )
            )

        return dal.upsert_activities(rows)
    except GarthHTTPError as e:
        print(f"  Error fetching activities: {e}")
        return 0


def sync_training_status(client: Garmin, target_date: str) -> int:
    """Sync training status for a specific date."""
    try:
        data = client.get_training_status(target_date)
        if not data:
            return 0

        dal.upsert_training_status(
            [
                (
                    target_date,
                    data.get("trainingStatusLabel"),
                    data.get("trainingStatusPhrase"),
                    data.get("vo2MaxPreciseValue"),
                    data.get("cyclingVo2MaxPreciseValue"),
                    data.get("loadLast7Days"),
                    data.get("loadLast28Days"),
                    data.get("recoveryTime"),
                    json.dumps(data),
                )
            ]
        )
        return 1
    except GarthHTTPError as e:
        print(f"  Error fetching training status: {e}")
        return 0


# ---------------------------------------------------------------------------
# Main sync orchestration
# ---------------------------------------------------------------------------


def sync_date(client: Garmin, target_date: str) -> dict:
    """Sync all metrics for a specific date."""
    results = {}

    print(f"Syncing {target_date}...")

    results["heart_rate"] = sync_heart_rate(client, target_date)
    print(f"  Heart rate: {results['heart_rate']} records")

    results["stress"] = sync_stress(client, target_date)
    print(f"  Stress: {results['stress']} records")

    results["body_battery"] = sync_body_battery(client, target_date)
    print(f"  Body battery: {results['body_battery']} records")

    results["sleep"] = sync_sleep(client, target_date)
    print(f"  Sleep: {results['sleep']} records")

    results["spo2"] = sync_spo2(client, target_date)
    print(f"  SpO2: {results['spo2']} records")

    results["respiration"] = sync_respiration(client, target_date)
    print(f"  Respiration: {results['respiration']} records")

    results["hrv"] = sync_hrv(client, target_date)
    print(f"  HRV: {results['hrv']} records")

    results["steps"] = sync_steps(client, target_date)
    print(f"  Steps/Daily: {results['steps']} records")

    results["training"] = sync_training_status(client, target_date)
    print(f"  Training status: {results['training']} records")

    return results


def main():
    parser = argparse.ArgumentParser(description="Sync Garmin Connect data to PostgreSQL")
    parser.add_argument(
        "--login",
        nargs=2,
        metavar=("EMAIL", "PASSWORD"),
        help="Initial login with credentials",
    )
    parser.add_argument("--date", type=str, help="Sync specific date (YYYY-MM-DD)")
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Number of days to sync (default: 1, today)",
    )
    parser.add_argument(
        "--activities",
        type=int,
        default=10,
        help="Number of recent activities to sync",
    )
    args = parser.parse_args()

    email, password = None, None
    prompt_mfa = False
    if args.login:
        email, password = args.login
        prompt_mfa = True

    client = get_client(email, password, prompt_mfa=prompt_mfa)

    if args.date:
        dates = [args.date]
    else:
        dates = [(date.today() - timedelta(days=i)).isoformat() for i in range(args.days)]

    total = {"total": 0}
    for d in dates:
        results = sync_date(client, d)
        for metric, count in results.items():
            total[metric] = total.get(metric, 0) + count
            total["total"] += count

    print(f"\nSyncing last {args.activities} activities...")
    act_count = sync_activities(client, args.activities)
    print(f"  Activities: {act_count} records")
    total["activities"] = act_count
    total["total"] += act_count

    dal.log_sync("full_sync", total["total"])

    print(f"\nSync complete. Total records: {total['total']}")


if __name__ == "__main__":
    main()
