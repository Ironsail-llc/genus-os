"""Buddy — gamification engine for Robothor.

Tracks RPG stats, XP, levels, and streaks computed from real agent execution
data in the agent_runs and autodream_runs tables. No shadow bookkeeping — all
metrics derived from existing data on read, cached daily in buddy_stats.

Supports both **global** stats (the original Robothor "team level") and
**per-agent** stats stored in agent_buddy_stats for the fleet leaderboard.

Usage:
    engine = BuddyEngine()
    stats = engine.compute_daily_stats()                  # global
    stats = engine.compute_daily_stats(agent_id="main")   # per-agent
    fleet = engine.compute_fleet_scores()                  # all agents ranked
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

# Overall score weights (must sum to 1.0)
WEIGHT_RELIABILITY = 0.30
WEIGHT_DEBUGGING = 0.25
WEIGHT_PATIENCE = 0.20
WEIGHT_WISDOM = 0.15
WEIGHT_CHAOS = 0.10  # applied as (100 - chaos_score)


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


def level_name(level: int) -> str:
    """Thematic name based on level range."""
    if level < 5:
        return "Spark"
    if level < 10:
        return "Flame"
    if level < 20:
        return "Blaze"
    if level < 35:
        return "Inferno"
    if level < 50:
        return "Thunderstrike"
    return "Eternal Storm"


def compute_overall_score(
    debugging: int,
    patience: int,
    chaos: int,
    wisdom: int,
    reliability: int,
) -> int:
    """Weighted composite score (0-100). Chaos is inverted (low chaos = good)."""
    raw = (
        reliability * WEIGHT_RELIABILITY
        + debugging * WEIGHT_DEBUGGING
        + patience * WEIGHT_PATIENCE
        + wisdom * WEIGHT_WISDOM
        + (100 - chaos) * WEIGHT_CHAOS
    )
    return min(100, max(0, int(raw)))


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
        return level_name(self.level)


@dataclass
class AgentBuddyStats:
    """Per-agent RPG stats for one day."""

    agent_id: str
    stat_date: date
    tasks_completed: int = 0
    errors_recovered: int = 0
    debugging_score: int = 50
    patience_score: int = 50
    chaos_score: int = 50
    wisdom_score: int = 50
    reliability_score: int = 50
    overall_score: int = 50
    daily_xp: int = 0
    total_xp: int = 0
    level: int = 1
    rank: int = 0
    last_benchmark_score: float | None = None
    last_benchmark_at: datetime | None = None

    @property
    def level_name(self) -> str:
        return level_name(self.level)


# ── SQL helpers for optional agent_id filtering ────────────────────────────


def _agent_clause(agent_id: str | None, *, column: str = "agent_id") -> tuple[str, list[str]]:
    """Return (SQL AND-clause, params list) to filter by agent_id when provided.

    Use ``column`` to qualify the column for JOINs, e.g. ``column="r.agent_id"``.
    """
    if agent_id is not None:
        return " AND " + column + " = %s", [agent_id]
    return "", []


# ── Buddy Engine ────────────────────────────────────────────────────────────


class BuddyEngine:
    """Computes Robothor's gamification stats from real execution data."""

    def compute_daily_stats(
        self, target_date: date | None = None, *, agent_id: str | None = None
    ) -> DailyStats:
        """Compute stats for a given date from agent_runs + autodream_runs.

        When agent_id is provided, scopes all queries to that agent.
        When None, computes global (system-wide) stats.
        """
        if target_date is None:
            target_date = datetime.now(UTC).date()

        stats = DailyStats(stat_date=target_date)
        ac, ac_params = _agent_clause(agent_id)
        ac_r, ac_r_params = _agent_clause(agent_id, column="r.agent_id")

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
                    """
                    + ac,
                    [target_date, *ac_params],
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
                    """
                    + ac_r,
                    [target_date, *ac_r_params],
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
                    """
                    + ac,
                    [target_date, *ac_params],
                )
                row = cur.fetchone()
                stats.errors_avoided = int(row[0]) if row else 0

                # Count autoDream completions (global only — dreams are system-wide)
                if agent_id is None:
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
        stats.debugging_score = self._compute_debugging(target_date, agent_id=agent_id)
        stats.patience_score = self._compute_patience(target_date, agent_id=agent_id)
        stats.chaos_score = self._compute_chaos(target_date, agent_id=agent_id)
        stats.wisdom_score = self._compute_wisdom(stats)
        stats.reliability_score = self._compute_reliability(target_date, agent_id=agent_id)

        return stats

    def _compute_debugging(self, target_date: date, *, agent_id: str | None = None) -> int:
        """Debugging: error recovery rate over last 7 days."""
        try:
            from robothor.db.connection import get_connection

            start = target_date - timedelta(days=7)
            ac, ac_params = _agent_clause(agent_id)
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE error_message IS NOT NULL) as total_errors,
                        COUNT(*) FILTER (WHERE error_message IS NOT NULL AND status = 'completed') as recovered
                    FROM agent_runs
                    WHERE DATE(started_at AT TIME ZONE 'America/New_York') BETWEEN %s AND %s
                    """
                    + ac,
                    [start, target_date, *ac_params],
                )
                row = cur.fetchone()
                if row and row[0] > 0:
                    return min(100, int(100 * row[1] / row[0]))
        except Exception:
            pass
        return 50

    def _compute_patience(self, target_date: date, *, agent_id: str | None = None) -> int:
        """Patience: ratio of avg duration to timeout (consistent, not rushed)."""
        try:
            from robothor.db.connection import get_connection

            start = target_date - timedelta(days=7)
            ac, ac_params = _agent_clause(agent_id)
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT AVG(duration_ms), STDDEV(duration_ms)
                    FROM agent_runs
                    WHERE DATE(started_at AT TIME ZONE 'America/New_York') BETWEEN %s AND %s
                      AND status = 'completed' AND duration_ms > 0
                    """
                    + ac,
                    [start, target_date, *ac_params],
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

    def _compute_chaos(self, target_date: date, *, agent_id: str | None = None) -> int:
        """Chaos: inverse of outcome consistency (high variance = high chaos)."""
        try:
            from robothor.db.connection import get_connection

            start = target_date - timedelta(days=7)
            ac, ac_params = _agent_clause(agent_id)
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT status, COUNT(*) FROM agent_runs
                    WHERE DATE(started_at AT TIME ZONE 'America/New_York') BETWEEN %s AND %s
                    """
                    + ac
                    + """
                    GROUP BY status
                    """,
                    [start, target_date, *ac_params],
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

    def _compute_reliability(self, target_date: date, *, agent_id: str | None = None) -> int:
        """Reliability: completion rate over last 7 days."""
        try:
            from robothor.db.connection import get_connection

            start = target_date - timedelta(days=7)
            ac, ac_params = _agent_clause(agent_id)
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE status = 'completed') as completed
                    FROM agent_runs
                    WHERE DATE(started_at AT TIME ZONE 'America/New_York') BETWEEN %s AND %s
                    """
                    + ac,
                    [start, target_date, *ac_params],
                )
                row = cur.fetchone()
                if row and row[0] > 0:
                    return min(100, int(100 * row[1] / row[0]))
        except Exception:
            pass
        return 50

    # ── Per-agent scoring ──────────────────────────────────────────────────

    # Minimum runs in the 7-day window before we trust per-agent scores.
    # Below this threshold, scores stay at None (unranked) rather than 50.
    MIN_RUNS_FOR_SCORING = 3

    def _agent_run_count(self, agent_id: str, target_date: date) -> int:
        """Count completed runs for an agent in the last 7 days."""
        try:
            from robothor.db.connection import get_connection

            start = target_date - timedelta(days=7)
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT COUNT(*) FROM agent_runs
                    WHERE agent_id = %s
                      AND DATE(started_at AT TIME ZONE 'America/New_York') BETWEEN %s AND %s
                    """,
                    (agent_id, start, target_date),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    def compute_agent_scores(
        self, agent_id: str, target_date: date | None = None
    ) -> AgentBuddyStats | None:
        """Compute RPG scores for a single agent on a given date.

        Returns None if the agent has fewer than MIN_RUNS_FOR_SCORING runs
        in the 7-day window (not enough data for meaningful scores).
        """
        if target_date is None:
            target_date = datetime.now(UTC).date()

        # Check minimum data threshold
        run_count = self._agent_run_count(agent_id, target_date)
        if run_count < self.MIN_RUNS_FOR_SCORING:
            return None

        stats = self.compute_daily_stats(target_date, agent_id=agent_id)
        # AutoDreams are system-wide, not per-agent, so wisdom has no signal here.
        # Use neutral default (50) instead of the 0 that _compute_wisdom produces.
        stats.wisdom_score = 50
        daily_xp = (
            stats.tasks_completed * XP_TASK_COMPLETED + stats.errors_avoided * XP_ERROR_RECOVERY
        )

        # Get accumulated XP from agent_buddy_stats history
        accumulated_xp = self._get_agent_total_xp(agent_id, exclude_date=target_date)
        new_total_xp = accumulated_xp + daily_xp

        # Read latest benchmark score if available
        benchmark_score, benchmark_at = self._get_latest_benchmark(agent_id)

        return AgentBuddyStats(
            agent_id=agent_id,
            stat_date=target_date,
            tasks_completed=stats.tasks_completed,
            errors_recovered=stats.errors_avoided,
            debugging_score=stats.debugging_score,
            patience_score=stats.patience_score,
            chaos_score=stats.chaos_score,
            wisdom_score=stats.wisdom_score,
            reliability_score=stats.reliability_score,
            overall_score=compute_overall_score(
                stats.debugging_score,
                stats.patience_score,
                stats.chaos_score,
                stats.wisdom_score,
                stats.reliability_score,
            ),
            daily_xp=daily_xp,
            total_xp=new_total_xp,
            level=level_from_xp(new_total_xp),
            last_benchmark_score=benchmark_score,
            last_benchmark_at=benchmark_at,
        )

    def _get_agent_total_xp(self, agent_id: str, exclude_date: date | None = None) -> int:
        """Sum all daily_xp for an agent from agent_buddy_stats, optionally excluding a date."""
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                if exclude_date:
                    cur.execute(
                        "SELECT COALESCE(SUM(daily_xp), 0) FROM agent_buddy_stats "
                        "WHERE agent_id = %s AND stat_date != %s",
                        (agent_id, exclude_date),
                    )
                else:
                    cur.execute(
                        "SELECT COALESCE(SUM(daily_xp), 0) FROM agent_buddy_stats "
                        "WHERE agent_id = %s",
                        (agent_id,),
                    )
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    def _get_latest_benchmark(self, agent_id: str) -> tuple[float | None, datetime | None]:
        """Read the latest benchmark score from the memory block."""
        try:
            import json

            from robothor.memory.blocks import read_block

            data = read_block(f"agent_benchmark_latest:{agent_id}")
            if data and data.get("content"):
                parsed = json.loads(data["content"])
                score = float(parsed["aggregate_score"])
                ts = datetime.fromisoformat(parsed["timestamp"])
                return score, ts
        except Exception:
            pass
        return None, None

    def compute_fleet_scores(self, target_date: date | None = None) -> list[AgentBuddyStats]:
        """Compute RPG scores for all active agents, sorted by overall_score desc."""
        if target_date is None:
            target_date = datetime.now(UTC).date()

        agent_ids = self._get_active_agent_ids()
        results: list[AgentBuddyStats] = []

        for aid in agent_ids:
            try:
                agent_stats = self.compute_agent_scores(aid, target_date)
                if agent_stats is not None:
                    results.append(agent_stats)
                else:
                    logger.debug("Skipping agent %s: insufficient run data", aid)
            except Exception as e:
                logger.debug("Failed to compute scores for agent %s: %s", aid, e)

        # Sort by overall score descending and assign ranks
        results.sort(key=lambda s: s.overall_score, reverse=True)
        for i, s in enumerate(results, 1):
            s.rank = i

        return results

    def _get_active_agent_ids(self) -> list[str]:
        """Get list of agent_ids from agent_schedules."""
        try:
            from robothor.engine.tracking import list_schedules

            schedules = list_schedules(enabled_only=True)
            return [s["agent_id"] for s in schedules if s.get("cron_expr")]
        except Exception:
            return []

    def _upsert_agent_stats(self, stats: AgentBuddyStats) -> None:
        """Persist per-agent stats to agent_buddy_stats table."""
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO agent_buddy_stats (
                        agent_id, stat_date, tasks_completed, errors_recovered,
                        debugging_score, patience_score, chaos_score,
                        wisdom_score, reliability_score, overall_score,
                        daily_xp, total_xp, level,
                        last_benchmark_score, last_benchmark_at, computed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (agent_id, stat_date) DO UPDATE SET
                        tasks_completed = EXCLUDED.tasks_completed,
                        errors_recovered = EXCLUDED.errors_recovered,
                        debugging_score = EXCLUDED.debugging_score,
                        patience_score = EXCLUDED.patience_score,
                        chaos_score = EXCLUDED.chaos_score,
                        wisdom_score = EXCLUDED.wisdom_score,
                        reliability_score = EXCLUDED.reliability_score,
                        overall_score = EXCLUDED.overall_score,
                        daily_xp = EXCLUDED.daily_xp,
                        total_xp = EXCLUDED.total_xp,
                        level = EXCLUDED.level,
                        last_benchmark_score = EXCLUDED.last_benchmark_score,
                        last_benchmark_at = EXCLUDED.last_benchmark_at,
                        computed_at = NOW()
                    """,
                    (
                        stats.agent_id,
                        stats.stat_date,
                        stats.tasks_completed,
                        stats.errors_recovered,
                        stats.debugging_score,
                        stats.patience_score,
                        stats.chaos_score,
                        stats.wisdom_score,
                        stats.reliability_score,
                        stats.overall_score,
                        stats.daily_xp,
                        stats.total_xp,
                        stats.level,
                        stats.last_benchmark_score,
                        stats.last_benchmark_at,
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.warning("Failed to upsert agent buddy stats for %s: %s", stats.agent_id, e)

    # ── AutoAgent integration ──────────────────────────────────────────────

    def flag_underperformers(self, threshold: int = 70, consecutive_days: int = 2) -> list[str]:
        """Flag agents with consistently low scores for AutoAgent optimization.

        Returns list of agent_ids that were flagged (tasks created).
        """
        flagged: list[str] = []
        try:
            from robothor.db.connection import get_connection

            today = datetime.now(UTC).date()
            start = today - timedelta(days=consecutive_days - 1)

            with get_connection() as conn:
                cur = conn.cursor()
                # Find agents where ALL of the last N days have overall_score < threshold
                cur.execute(
                    """
                    SELECT agent_id
                    FROM agent_buddy_stats
                    WHERE stat_date BETWEEN %s AND %s
                      AND overall_score < %s
                    GROUP BY agent_id
                    HAVING COUNT(DISTINCT stat_date) >= %s
                    """,
                    (start, today, threshold, consecutive_days),
                )
                candidates = [row[0] for row in cur.fetchall()]

            if not candidates:
                return []

            flagged.extend(aid for aid in candidates if self._create_autoagent_task(aid, threshold))

        except Exception as e:
            logger.warning("Failed to flag underperformers: %s", e)

        return flagged

    def _create_autoagent_task(self, agent_id: str, threshold: int) -> bool:
        """Create a CRM task for AutoAgent to optimize a low-scoring agent.

        Checks for existing open tasks to avoid duplicates (7-day cooldown).
        """
        try:
            from robothor.crm.dal import create_task, list_tasks

            # Check for existing open autoagent task for this agent within 7 days
            existing = list_tasks(
                assigned_to_agent="auto-agent",
                status="TODO",
                tags=[agent_id],
            )
            if existing and not isinstance(existing, dict):
                return False  # Already has an open task

            # Get latest scores for context
            scores_str = ""
            try:
                from robothor.db.connection import get_connection

                with get_connection() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT overall_score, debugging_score, patience_score,
                               chaos_score, wisdom_score, reliability_score
                        FROM agent_buddy_stats
                        WHERE agent_id = %s
                        ORDER BY stat_date DESC LIMIT 1
                        """,
                        (agent_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        scores_str = (
                            f"Overall: {row[0]}, Debugging: {row[1]}, Patience: {row[2]}, "
                            f"Chaos: {row[3]}, Wisdom: {row[4]}, Reliability: {row[5]}"
                        )
            except Exception:
                pass

            create_task(
                title=f"Optimize {agent_id}: low score (below {threshold})",
                body=(
                    f"Agent {agent_id} has scored below {threshold} for 2+ consecutive days.\n\n"
                    f"**Current scores:** {scores_str}\n\n"
                    f"Define a benchmark suite (if none exists), run it, and iterate on "
                    f"the agent's instruction file and manifest to improve performance."
                ),
                assigned_to_agent="auto-agent",
                tags=["autoagent", "low-score", agent_id],
                priority="high",
            )
            logger.info("Created AutoAgent optimization task for %s", agent_id)
            return True

        except Exception as e:
            logger.warning("Failed to create autoagent task for %s: %s", agent_id, e)
            return False

    # ── Global stats (original behavior) ───────────────────────────────────

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
        Now also computes and persists per-agent scores and flags underperformers.

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

        # ── Per-agent scoring ──────────────────────────────────────────────
        try:
            fleet = self.compute_fleet_scores(target_date)
            for agent_stats in fleet:
                self._upsert_agent_stats(agent_stats)
            result["fleet_scores"] = len(fleet)
            logger.info("Refreshed per-agent buddy stats for %d agents", len(fleet))
        except Exception as e:
            logger.warning("Failed to refresh per-agent buddy stats: %s", e)
            result["fleet_error"] = str(e)

        # ── Flag underperformers for AutoAgent ─────────────────────────────
        try:
            flagged = self.flag_underperformers()
            if flagged:
                result["flagged_agents"] = flagged
                logger.info(
                    "Flagged %d agents for AutoAgent optimization: %s", len(flagged), flagged
                )
        except Exception as e:
            logger.debug("Failed to flag underperformers: %s", e)

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

    def increment_task_count(self, agent_id: str | None = None) -> None:
        """Lightweight counter increment — called by AGENT_END lifecycle hook.

        Only updates the counter column, not the computed scores.
        Increments both the global buddy_stats and per-agent agent_buddy_stats.
        """
        try:
            from robothor.db.connection import get_connection

            today = datetime.now(UTC).date()
            with get_connection() as conn:
                cur = conn.cursor()
                # Global counter
                cur.execute(
                    """
                    INSERT INTO buddy_stats (stat_date, tasks_completed)
                    VALUES (%s, 1)
                    ON CONFLICT (stat_date) DO UPDATE SET
                        tasks_completed = buddy_stats.tasks_completed + 1
                    """,
                    (today,),
                )
                # Per-agent counter
                if agent_id:
                    cur.execute(
                        """
                        INSERT INTO agent_buddy_stats (agent_id, stat_date, tasks_completed)
                        VALUES (%s, %s, 1)
                        ON CONFLICT (agent_id, stat_date) DO UPDATE SET
                            tasks_completed = agent_buddy_stats.tasks_completed + 1
                        """,
                        (agent_id, today),
                    )
                conn.commit()
        except Exception as e:
            logger.debug("Failed to increment task count: %s", e)
