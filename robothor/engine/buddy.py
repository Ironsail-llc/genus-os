"""Buddy — gamification engine for Robothor.

Tracks RPG stats, XP, levels, and streaks computed from real agent execution
data in the agent_runs and autodream_runs tables. No shadow bookkeeping — all
metrics derived from existing data on read, cached daily in buddy_stats.

Usage:
    engine = BuddyEngine()
    stats = engine.compute_daily_stats()
    level = engine.get_level_info()
    engine.refresh_daily()  # called by evening-winddown or autoDream deep mode
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


# ── XP Constants ────────────────────────────────────────────────────────────

XP_TASK_COMPLETED = 10
XP_EMAIL_PROCESSED = 5
XP_INSIGHT_GENERATED = 20
XP_ERROR_RECOVERY = 15
XP_DREAM_COMPLETED = 10
XP_STREAK_BONUS = 5  # per streak day, multiplicative


def xp_for_level(level: int) -> int:
    """XP threshold to reach a given level. Level 1 = 100, Level 10 = 1000."""
    return level * 100


def level_from_xp(total_xp: int) -> int:
    """Compute level from total XP. Inverse of xp_for_level cumulative sum."""
    # Sum of xp_for_level(1..n) = 100 * n*(n+1)/2
    # Solve: 100 * n*(n+1)/2 = total_xp → n ≈ sqrt(total_xp/50)
    if total_xp <= 0:
        return 1
    n = int((-1 + math.sqrt(1 + 8 * total_xp / 100)) / 2)
    return max(1, n)


# ── Data Classes ────────────────────────────────────────────────────────────


@dataclass
class DailyStats:
    """Computed daily statistics."""

    stat_date: date
    tasks_completed: int = 0
    emails_processed: int = 0
    insights_generated: int = 0
    errors_avoided: int = 0
    dreams_completed: int = 0
    # Computed RPG scores (0-100)
    debugging_score: int = 50
    patience_score: int = 50
    chaos_score: int = 50
    wisdom_score: int = 50
    reliability_score: int = 50

    def total_daily_xp(self, streak_days: int = 0) -> int:
        """Compute XP earned for this day."""
        base = (
            self.tasks_completed * XP_TASK_COMPLETED
            + self.emails_processed * XP_EMAIL_PROCESSED
            + self.insights_generated * XP_INSIGHT_GENERATED
            + self.errors_avoided * XP_ERROR_RECOVERY
            + self.dreams_completed * XP_DREAM_COMPLETED
        )
        streak_bonus = streak_days * XP_STREAK_BONUS if streak_days > 0 else 0
        return base + streak_bonus

    def summary(self) -> str:
        """One-line summary string."""
        return (
            f"Tasks:{self.tasks_completed} Emails:{self.emails_processed} "
            f"Insights:{self.insights_generated} Dreams:{self.dreams_completed}"
        )


@dataclass
class LevelInfo:
    """Current level and XP progression."""

    level: int
    total_xp: int
    xp_for_current_level: int
    xp_for_next_level: int
    progress_pct: float  # 0.0 - 1.0

    @property
    def level_name(self) -> str:
        """Thematic name based on level range."""
        if self.level < 5:
            return "Spark"
        if self.level < 10:
            return "Flame"
        if self.level < 20:
            return "Blaze"
        if self.level < 35:
            return "Inferno"
        if self.level < 50:
            return "Thunderstrike"
        return "Eternal Storm"


# ── Buddy Engine ────────────────────────────────────────────────────────────


class BuddyEngine:
    """Computes Robothor's gamification stats from real execution data."""

    def compute_daily_stats(self, target_date: date | None = None) -> DailyStats:
        """Compute stats for a given date from agent_runs + autodream_runs.

        Queries the actual execution tables — no shadow counters.
        """
        if target_date is None:
            target_date = datetime.now(UTC).date()

        stats = DailyStats(stat_date=target_date)

        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()

                # Count completed agent runs for this date
                cur.execute(
                    """
                    SELECT COUNT(*) FROM agent_runs
                    WHERE DATE(started_at AT TIME ZONE 'America/New_York') = %s
                      AND status = 'completed'
                    """,
                    (target_date,),
                )
                row = cur.fetchone()
                stats.tasks_completed = int(row[0]) if row else 0

                # Count email-related tool uses (proxy for emails processed)
                cur.execute(
                    """
                    SELECT COUNT(*) FROM agent_run_steps s
                    JOIN agent_runs r ON r.id = s.run_id
                    WHERE DATE(r.started_at AT TIME ZONE 'America/New_York') = %s
                      AND s.tool_name IN ('gws_gmail_search', 'gws_gmail_get', 'gws_gmail_send',
                                          'gws_gmail_modify', 'gmail_search_messages',
                                          'gmail_read_message', 'gmail_read_thread')
                    """,
                    (target_date,),
                )
                row = cur.fetchone()
                stats.emails_processed = int(row[0]) if row else 0

                # Count errors that were recovered (run had error steps but completed)
                cur.execute(
                    """
                    SELECT COUNT(*) FROM agent_runs
                    WHERE DATE(started_at AT TIME ZONE 'America/New_York') = %s
                      AND status = 'completed'
                      AND error_message IS NOT NULL
                    """,
                    (target_date,),
                )
                row = cur.fetchone()
                stats.errors_avoided = int(row[0]) if row else 0

                # Count autoDream completions
                cur.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(insights_discovered), 0)
                    FROM autodream_runs
                    WHERE DATE(started_at AT TIME ZONE 'America/New_York') = %s
                      AND completed_at IS NOT NULL
                      AND error_message IS NULL
                    """,
                    (target_date,),
                )
                row = cur.fetchone()
                if row:
                    stats.dreams_completed = int(row[0])
                    stats.insights_generated = int(row[1])

        except Exception as e:
            logger.warning("Failed to compute daily stats: %s", e)

        # Compute RPG scores
        stats.debugging_score = self._compute_debugging(target_date)
        stats.patience_score = self._compute_patience(target_date)
        stats.chaos_score = self._compute_chaos(target_date)
        stats.wisdom_score = self._compute_wisdom(stats)
        stats.reliability_score = self._compute_reliability(target_date)

        return stats

    def _compute_debugging(self, target_date: date) -> int:
        """Debugging: error recovery rate over last 7 days."""
        try:
            from robothor.db.connection import get_connection

            start = target_date - timedelta(days=7)
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE error_message IS NOT NULL) as total_errors,
                        COUNT(*) FILTER (WHERE error_message IS NOT NULL AND status = 'completed') as recovered
                    FROM agent_runs
                    WHERE DATE(started_at AT TIME ZONE 'America/New_York') BETWEEN %s AND %s
                    """,
                    (start, target_date),
                )
                row = cur.fetchone()
                if row and row[0] > 0:
                    return min(100, int(100 * row[1] / row[0]))
        except Exception:
            pass
        return 50

    def _compute_patience(self, target_date: date) -> int:
        """Patience: ratio of avg duration to timeout (consistent, not rushed)."""
        try:
            from robothor.db.connection import get_connection

            start = target_date - timedelta(days=7)
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT AVG(duration_ms), STDDEV(duration_ms)
                    FROM agent_runs
                    WHERE DATE(started_at AT TIME ZONE 'America/New_York') BETWEEN %s AND %s
                      AND status = 'completed' AND duration_ms > 0
                    """,
                    (start, target_date),
                )
                row = cur.fetchone()
                if row and row[0] and row[1]:
                    avg_ms = float(row[0])
                    std_ms = float(row[1])
                    # Low variance relative to mean = high patience
                    cv = std_ms / avg_ms if avg_ms > 0 else 1.0
                    return min(100, max(0, int(100 * (1.0 - min(cv, 1.0)))))
        except Exception:
            pass
        return 50

    def _compute_chaos(self, target_date: date) -> int:
        """Chaos: inverse of outcome consistency (high variance = high chaos)."""
        try:
            from robothor.db.connection import get_connection

            start = target_date - timedelta(days=7)
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT status, COUNT(*) FROM agent_runs
                    WHERE DATE(started_at AT TIME ZONE 'America/New_York') BETWEEN %s AND %s
                    GROUP BY status
                    """,
                    (start, target_date),
                )
                rows = cur.fetchall()
                if rows:
                    total = sum(r[1] for r in rows)
                    completed = sum(r[1] for r in rows if r[0] == "completed")
                    if total > 0:
                        completion_rate = completed / total
                        # High completion rate = low chaos
                        return min(100, max(0, int(100 * (1.0 - completion_rate))))
        except Exception:
            pass
        return 50

    def _compute_wisdom(self, stats: DailyStats) -> int:
        """Wisdom: insights and dreams weighted score."""
        raw = stats.insights_generated * 10 + stats.dreams_completed * 5
        return min(100, raw)

    def _compute_reliability(self, target_date: date) -> int:
        """Reliability: completion rate over last 7 days."""
        try:
            from robothor.db.connection import get_connection

            start = target_date - timedelta(days=7)
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE status = 'completed') as completed
                    FROM agent_runs
                    WHERE DATE(started_at AT TIME ZONE 'America/New_York') BETWEEN %s AND %s
                    """,
                    (start, target_date),
                )
                row = cur.fetchone()
                if row and row[0] > 0:
                    return min(100, int(100 * row[1] / row[0]))
        except Exception:
            pass
        return 50

    def get_streak(self, target_date: date | None = None) -> tuple[int, int]:
        """Compute current and longest streak from buddy_stats history.

        A streak day = a day where tasks_completed > 0.

        Returns:
            (current_streak_days, longest_streak_days)
        """
        if target_date is None:
            target_date = datetime.now(UTC).date()
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT stat_date, tasks_completed FROM buddy_stats
                    WHERE stat_date <= %s
                    ORDER BY stat_date DESC
                    LIMIT 365
                    """,
                    (target_date,),
                )
                rows = cur.fetchall()

            if not rows:
                return (0, 0)

            # Current streak: count consecutive days with tasks > 0 from today backwards
            current_streak = 0
            expected_date = target_date
            for row in rows:
                if row[0] == expected_date and row[1] > 0:
                    current_streak += 1
                    expected_date -= timedelta(days=1)
                else:
                    break

            # Longest streak: scan all rows
            longest = 0
            streak = 0
            prev_date = None
            for row in reversed(rows):
                if row[1] > 0:
                    if prev_date is None or row[0] == prev_date + timedelta(days=1):
                        streak += 1
                    else:
                        streak = 1
                    longest = max(longest, streak)
                else:
                    streak = 0
                prev_date = row[0]

            return (current_streak, max(longest, current_streak))
        except Exception:
            return (0, 0)

    def get_level_info(self) -> LevelInfo:
        """Get current level and XP from buddy_profile."""
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT total_xp, level FROM buddy_profile WHERE id = 1")
                row = cur.fetchone()
                if row:
                    total_xp = int(row[0])
                    level = level_from_xp(total_xp)
                    # XP accumulated for levels 1..level
                    xp_at_current = sum(xp_for_level(i) for i in range(1, level + 1))
                    xp_at_next = xp_at_current + xp_for_level(level + 1)
                    progress = (total_xp - xp_at_current) / max(1, xp_at_next - xp_at_current)
                    return LevelInfo(
                        level=level,
                        total_xp=total_xp,
                        xp_for_current_level=xp_at_current,
                        xp_for_next_level=xp_at_next,
                        progress_pct=min(1.0, max(0.0, progress)),
                    )
        except Exception as e:
            logger.warning("Failed to get level info: %s", e)

        return LevelInfo(
            level=1, total_xp=0, xp_for_current_level=0, xp_for_next_level=100, progress_pct=0.0
        )

    def refresh_daily(self, target_date: date | None = None) -> dict[str, Any]:
        """Compute and persist daily stats, update XP and level.

        Called once/day by evening-winddown agent or autoDream deep mode.

        Returns:
            Dict with stats summary and level-up info.
        """
        if target_date is None:
            target_date = datetime.now(UTC).date()

        stats = self.compute_daily_stats(target_date)
        current_streak, longest_streak = self.get_streak(target_date)
        daily_xp = stats.total_daily_xp(streak_days=current_streak)

        level_before = self.get_level_info()
        result: dict[str, Any] = {"date": str(target_date), "daily_xp": daily_xp}

        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()

                # Upsert buddy_stats for this date
                cur.execute(
                    """
                    INSERT INTO buddy_stats (
                        stat_date, tasks_completed, emails_processed,
                        insights_generated, errors_avoided, dreams_completed,
                        debugging_score, patience_score, chaos_score,
                        wisdom_score, reliability_score,
                        total_xp, level, current_streak_days, longest_streak_days
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (stat_date) DO UPDATE SET
                        tasks_completed = EXCLUDED.tasks_completed,
                        emails_processed = EXCLUDED.emails_processed,
                        insights_generated = EXCLUDED.insights_generated,
                        errors_avoided = EXCLUDED.errors_avoided,
                        dreams_completed = EXCLUDED.dreams_completed,
                        debugging_score = EXCLUDED.debugging_score,
                        patience_score = EXCLUDED.patience_score,
                        chaos_score = EXCLUDED.chaos_score,
                        wisdom_score = EXCLUDED.wisdom_score,
                        reliability_score = EXCLUDED.reliability_score,
                        total_xp = EXCLUDED.total_xp,
                        level = EXCLUDED.level,
                        current_streak_days = EXCLUDED.current_streak_days,
                        longest_streak_days = EXCLUDED.longest_streak_days
                    """,
                    (
                        target_date,
                        stats.tasks_completed,
                        stats.emails_processed,
                        stats.insights_generated,
                        stats.errors_avoided,
                        stats.dreams_completed,
                        stats.debugging_score,
                        stats.patience_score,
                        stats.chaos_score,
                        stats.wisdom_score,
                        stats.reliability_score,
                        daily_xp,
                        level_before.level,
                        current_streak,
                        longest_streak,
                    ),
                )

                # Compute total XP idempotently from buddy_stats sum
                cur.execute("SELECT COALESCE(SUM(total_xp), 0) FROM buddy_stats")
                xp_row = cur.fetchone()
                new_total_xp = int(xp_row[0]) if xp_row else 0
                new_level = level_from_xp(new_total_xp)
                cur.execute(
                    """
                    UPDATE buddy_profile SET
                        total_xp = %s, level = %s, updated_at = NOW()
                    WHERE id = 1
                    """,
                    (new_total_xp, new_level),
                )
                conn.commit()

                result["new_total_xp"] = new_total_xp
                result["new_level"] = new_level
                result["leveled_up"] = new_level > level_before.level
                result["stats"] = {
                    "tasks": stats.tasks_completed,
                    "emails": stats.emails_processed,
                    "insights": stats.insights_generated,
                    "dreams": stats.dreams_completed,
                    "streak": current_streak,
                }

        except Exception as e:
            logger.warning("Failed to refresh daily buddy stats: %s", e)
            result["error"] = str(e)

        # Update memory block
        self._update_status_block(stats, daily_xp, current_streak, level_before)

        return result

    def _update_status_block(
        self, stats: DailyStats, daily_xp: int, streak: int, level: LevelInfo
    ) -> None:
        """Write buddy_status memory block for warmup injection."""
        try:
            from robothor.memory.blocks import write_block

            lines = [
                f"Level {level.level} {level.level_name} ({level.total_xp + daily_xp:,} XP) | {streak}-day streak",
                f"Debugging: {stats.debugging_score} | Patience: {stats.patience_score} | "
                f"Chaos: {stats.chaos_score} | Wisdom: {stats.wisdom_score} | "
                f"Reliability: {stats.reliability_score}",
                f"Today: {stats.tasks_completed} tasks, {stats.emails_processed} emails, "
                f"{stats.insights_generated} insights, {stats.dreams_completed} dreams (+{daily_xp} XP)",
            ]
            write_block("buddy_status", "\n".join(lines))
        except Exception as e:
            logger.debug("Failed to update buddy_status block: %s", e)

    def increment_task_count(self) -> None:
        """Lightweight counter increment — called by AGENT_END lifecycle hook.

        Only updates the counter column, not the computed scores.
        """
        try:
            from robothor.db.connection import get_connection

            today = datetime.now(UTC).date()
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO buddy_stats (stat_date, tasks_completed)
                    VALUES (%s, 1)
                    ON CONFLICT (stat_date) DO UPDATE SET
                        tasks_completed = buddy_stats.tasks_completed + 1
                    """,
                    (today,),
                )
                conn.commit()
        except Exception as e:
            logger.debug("Failed to increment task count: %s", e)
