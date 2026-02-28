"""
Data access layer for Garmin health tables in PostgreSQL.

All writes use ON CONFLICT DO UPDATE (upsert) for idempotent sync.
All reads return plain dicts/lists for easy consumption.
"""

from __future__ import annotations

import logging
from datetime import datetime

import psycopg2.extras

from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_timestamp(ts) -> int | None:
    """Convert various timestamp formats to Unix seconds."""
    if ts is None:
        return None
    if isinstance(ts, int):
        return ts // 1000 if ts > 10_000_000_000 else ts
    if isinstance(ts, float):
        return int(ts)
    if isinstance(ts, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(ts.replace("Z", "").split("+")[0], fmt)
                return int(dt.timestamp())
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Batch upserts (for sync)
# ---------------------------------------------------------------------------


def upsert_heart_rate(rows: list[tuple]) -> int:
    """Upsert (timestamp, heart_rate, source) rows."""
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO health_heart_rate (timestamp, heart_rate, source)
                   VALUES %s
                   ON CONFLICT (timestamp) DO UPDATE
                   SET heart_rate = EXCLUDED.heart_rate,
                       source = EXCLUDED.source""",
                rows,
            )
    return len(rows)


def upsert_resting_heart_rate(rows: list[tuple]) -> int:
    """Upsert (date, resting_hr, timestamp) rows."""
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO health_resting_heart_rate (date, resting_hr, timestamp)
                   VALUES %s
                   ON CONFLICT (date) DO UPDATE
                   SET resting_hr = EXCLUDED.resting_hr,
                       timestamp = EXCLUDED.timestamp""",
                rows,
            )
    return len(rows)


def upsert_stress(rows: list[tuple]) -> int:
    """Upsert (timestamp, stress_level) rows."""
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO health_stress (timestamp, stress_level)
                   VALUES %s
                   ON CONFLICT (timestamp) DO UPDATE
                   SET stress_level = EXCLUDED.stress_level""",
                rows,
            )
    return len(rows)


def upsert_body_battery(rows: list[tuple]) -> int:
    """Upsert (timestamp, level, charged, drained) rows."""
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO health_body_battery (timestamp, level, charged, drained)
                   VALUES %s
                   ON CONFLICT (timestamp) DO UPDATE
                   SET level = EXCLUDED.level,
                       charged = EXCLUDED.charged,
                       drained = EXCLUDED.drained""",
                rows,
            )
    return len(rows)


def upsert_sleep(rows: list[tuple]) -> int:
    """Upsert (date, start_timestamp, end_timestamp, total_sleep_seconds,
    deep_sleep_seconds, light_sleep_seconds, rem_sleep_seconds, awake_seconds,
    score, quality, raw_data) rows."""
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO health_sleep
                   (date, start_timestamp, end_timestamp, total_sleep_seconds,
                    deep_sleep_seconds, light_sleep_seconds, rem_sleep_seconds,
                    awake_seconds, score, quality, raw_data)
                   VALUES %s
                   ON CONFLICT (date) DO UPDATE
                   SET start_timestamp = EXCLUDED.start_timestamp,
                       end_timestamp = EXCLUDED.end_timestamp,
                       total_sleep_seconds = EXCLUDED.total_sleep_seconds,
                       deep_sleep_seconds = EXCLUDED.deep_sleep_seconds,
                       light_sleep_seconds = EXCLUDED.light_sleep_seconds,
                       rem_sleep_seconds = EXCLUDED.rem_sleep_seconds,
                       awake_seconds = EXCLUDED.awake_seconds,
                       score = EXCLUDED.score,
                       quality = EXCLUDED.quality,
                       raw_data = EXCLUDED.raw_data""",
                rows,
            )
    return len(rows)


def upsert_spo2(rows: list[tuple]) -> int:
    """Upsert (timestamp, spo2_value, reading_type) rows."""
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO health_spo2 (timestamp, spo2_value, reading_type)
                   VALUES %s
                   ON CONFLICT (timestamp) DO UPDATE
                   SET spo2_value = EXCLUDED.spo2_value,
                       reading_type = EXCLUDED.reading_type""",
                rows,
            )
    return len(rows)


def upsert_respiration(rows: list[tuple]) -> int:
    """Upsert (timestamp, respiration_rate) rows."""
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO health_respiration (timestamp, respiration_rate)
                   VALUES %s
                   ON CONFLICT (timestamp) DO UPDATE
                   SET respiration_rate = EXCLUDED.respiration_rate""",
                rows,
            )
    return len(rows)


def upsert_hrv(rows: list[tuple]) -> int:
    """Upsert (timestamp, hrv_value, reading_type, status) rows."""
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO health_hrv (timestamp, hrv_value, reading_type, status)
                   VALUES %s
                   ON CONFLICT (timestamp) DO UPDATE
                   SET hrv_value = EXCLUDED.hrv_value,
                       reading_type = EXCLUDED.reading_type,
                       status = EXCLUDED.status""",
                rows,
            )
    return len(rows)


def upsert_steps(rows: list[tuple]) -> int:
    """Upsert (date, total_steps, goal, distance_meters, calories, timestamp) rows."""
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO health_steps
                   (date, total_steps, goal, distance_meters, calories, timestamp)
                   VALUES %s
                   ON CONFLICT (date) DO UPDATE
                   SET total_steps = EXCLUDED.total_steps,
                       goal = EXCLUDED.goal,
                       distance_meters = EXCLUDED.distance_meters,
                       calories = EXCLUDED.calories,
                       timestamp = EXCLUDED.timestamp""",
                rows,
            )
    return len(rows)


def upsert_daily_summary(rows: list[tuple]) -> int:
    """Upsert (date, calories_total, calories_active, calories_bmr,
    floors_climbed, intensity_minutes, raw_data) rows."""
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO health_daily_summary
                   (date, calories_total, calories_active, calories_bmr,
                    floors_climbed, intensity_minutes, raw_data)
                   VALUES %s
                   ON CONFLICT (date) DO UPDATE
                   SET calories_total = EXCLUDED.calories_total,
                       calories_active = EXCLUDED.calories_active,
                       calories_bmr = EXCLUDED.calories_bmr,
                       floors_climbed = EXCLUDED.floors_climbed,
                       intensity_minutes = EXCLUDED.intensity_minutes,
                       raw_data = EXCLUDED.raw_data""",
                rows,
            )
    return len(rows)


def upsert_training_status(rows: list[tuple]) -> int:
    """Upsert (date, training_status, training_status_phrase, vo2max_running,
    vo2max_cycling, training_load_7_day, training_load_28_day,
    recovery_time_hours, raw_data) rows."""
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO health_training_status
                   (date, training_status, training_status_phrase, vo2max_running,
                    vo2max_cycling, training_load_7_day, training_load_28_day,
                    recovery_time_hours, raw_data)
                   VALUES %s
                   ON CONFLICT (date) DO UPDATE
                   SET training_status = EXCLUDED.training_status,
                       training_status_phrase = EXCLUDED.training_status_phrase,
                       vo2max_running = EXCLUDED.vo2max_running,
                       vo2max_cycling = EXCLUDED.vo2max_cycling,
                       training_load_7_day = EXCLUDED.training_load_7_day,
                       training_load_28_day = EXCLUDED.training_load_28_day,
                       recovery_time_hours = EXCLUDED.recovery_time_hours,
                       raw_data = EXCLUDED.raw_data""",
                rows,
            )
    return len(rows)


def upsert_activities(rows: list[tuple]) -> int:
    """Upsert (activity_id, name, activity_type, start_timestamp, duration_seconds,
    distance_meters, calories, avg_heart_rate, max_heart_rate, avg_pace,
    elevation_gain, vo2max, training_effect_aerobic, training_effect_anaerobic,
    training_load, raw_data) rows."""
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO health_activities
                   (activity_id, name, activity_type, start_timestamp, duration_seconds,
                    distance_meters, calories, avg_heart_rate, max_heart_rate, avg_pace,
                    elevation_gain, vo2max, training_effect_aerobic,
                    training_effect_anaerobic, training_load, raw_data)
                   VALUES %s
                   ON CONFLICT (activity_id) DO UPDATE
                   SET name = EXCLUDED.name,
                       activity_type = EXCLUDED.activity_type,
                       start_timestamp = EXCLUDED.start_timestamp,
                       duration_seconds = EXCLUDED.duration_seconds,
                       distance_meters = EXCLUDED.distance_meters,
                       calories = EXCLUDED.calories,
                       avg_heart_rate = EXCLUDED.avg_heart_rate,
                       max_heart_rate = EXCLUDED.max_heart_rate,
                       avg_pace = EXCLUDED.avg_pace,
                       elevation_gain = EXCLUDED.elevation_gain,
                       vo2max = EXCLUDED.vo2max,
                       training_effect_aerobic = EXCLUDED.training_effect_aerobic,
                       training_effect_anaerobic = EXCLUDED.training_effect_anaerobic,
                       training_load = EXCLUDED.training_load,
                       raw_data = EXCLUDED.raw_data""",
                rows,
            )
    return len(rows)


def log_sync(
    metric_type: str, records: int, status: str = "success", error: str | None = None
) -> None:
    """Log a sync operation."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO health_sync_log
                   (sync_timestamp, metric_type, records_synced, status, error_message)
                   VALUES (%s, %s, %s, %s, %s)""",
                (int(datetime.now().timestamp()), metric_type, records, status, error),
            )


# ---------------------------------------------------------------------------
# Queries (for summary)
# ---------------------------------------------------------------------------


def get_sleep(today: str, yesterday: str) -> dict:
    """Get last night's sleep. Try today first, then yesterday."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            for d in (today, yesterday):
                cur.execute(
                    """SELECT total_sleep_seconds, deep_sleep_seconds,
                              light_sleep_seconds, rem_sleep_seconds,
                              score, quality
                       FROM health_sleep WHERE date = %s""",
                    (d,),
                )
                row = cur.fetchone()
                if row and row[0] is not None:
                    return {
                        "total": row[0],
                        "deep": row[1],
                        "light": row[2],
                        "rem": row[3],
                        "score": row[4],
                        "quality": row[5],
                    }
    return {}


def get_body_battery(start_ts: int, end_ts: int) -> dict:
    """Get current body battery level and today's peak."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT level FROM health_body_battery
                   WHERE timestamp >= %s AND timestamp <= %s
                   ORDER BY timestamp DESC LIMIT 1""",
                (start_ts, end_ts),
            )
            row = cur.fetchone()
            current = row[0] if row else None

            cur.execute(
                """SELECT MAX(level) FROM health_body_battery
                   WHERE timestamp >= %s AND timestamp <= %s""",
                (start_ts, end_ts),
            )
            row = cur.fetchone()
            peak = row[0] if row else None

    return {"current": current, "peak": peak}


def get_stress_avg(start_ts: int, end_ts: int) -> dict:
    """Get today's average and peak stress."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT AVG(stress_level), MAX(stress_level)
                   FROM health_stress
                   WHERE timestamp >= %s AND timestamp <= %s""",
                (start_ts, end_ts),
            )
            row = cur.fetchone()
            avg = round(row[0]) if row and row[0] is not None else None
            peak = row[1] if row else None
    return {"avg": avg, "peak": peak}


def get_steps(date_str: str) -> dict:
    """Get today's steps and goal."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT total_steps, goal FROM health_steps WHERE date = %s",
                (date_str,),
            )
            row = cur.fetchone()
            if row and row[0] is not None:
                total = row[0]
                goal = row[1]
                pct = round(total / goal * 100) if goal and goal > 0 else None
                return {"total": total, "goal": goal, "pct": pct}
    return {}


def get_resting_hr(today: str, yesterday: str) -> int | None:
    """Get resting heart rate. Try today, then yesterday."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            for d in (today, yesterday):
                cur.execute(
                    "SELECT resting_hr FROM health_resting_heart_rate WHERE date = %s",
                    (d,),
                )
                row = cur.fetchone()
                if row and row[0] is not None:
                    return row[0]
    return None


def get_hrv_latest(start_ts: int, end_ts: int) -> int | None:
    """Get latest overnight HRV reading."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT hrv_value FROM health_hrv
                   WHERE timestamp >= %s AND timestamp <= %s
                   AND reading_type = 'reading'
                   ORDER BY timestamp DESC LIMIT 1""",
                (start_ts, end_ts),
            )
            row = cur.fetchone()
            return round(row[0]) if row and row[0] is not None else None
