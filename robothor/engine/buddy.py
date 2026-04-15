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

import json
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
WEIGHT_RELIABILITY = 0.25
WEIGHT_DEBUGGING = 0.20
WEIGHT_PATIENCE = 0.15
WEIGHT_EFFECTIVENESS = 0.25  # outcome quality (was WEIGHT_CHAOS)
WEIGHT_BENCHMARK = 0.15  # benchmark aggregate score (was WEIGHT_WISDOM)


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
    effectiveness: int,
    benchmark_score: int,
    reliability: int,
) -> int:
    """Weighted composite score (0-100)."""
    raw = (
        reliability * WEIGHT_RELIABILITY
        + debugging * WEIGHT_DEBUGGING
        + patience * WEIGHT_PATIENCE
        + effectiveness * WEIGHT_EFFECTIVENESS
        + benchmark_score * WEIGHT_BENCHMARK
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
    chaos_score: int = 50  # legacy, kept for DB compat
    wisdom_score: int = 50  # legacy, kept for DB compat
    effectiveness_score: int = 50  # outcome quality (replaces chaos in overall)
    benchmark_dim_score: int = 50  # benchmark aggregate (replaces wisdom in overall)
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
    chaos_score: int = 50  # legacy, kept for DB compat
    wisdom_score: int = 50  # legacy, kept for DB compat
    effectiveness_score: int = 50  # outcome quality (replaces chaos in overall)
    benchmark_dim_score: int = 50  # benchmark aggregate (replaces wisdom in overall)
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
        stats.effectiveness_score = self._compute_effectiveness(target_date, agent_id=agent_id)
        stats.reliability_score = self._compute_reliability(target_date, agent_id=agent_id)

        return stats

    def _compute_debugging(self, target_date: date, *, agent_id: str | None = None) -> int:
        """Debugging: error recovery rate over last 7 days.

        No errors in the window = perfect score (100), not neutral (50).
        Errors present = recovery rate as percentage.
        """
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
                if row:
                    total_errors = int(row[0])
                    if total_errors == 0:
                        return 100  # No errors = perfect
                    recovered = int(row[1])
                    return min(100, int(100 * recovered / total_errors))
        except Exception:
            pass
        return 50

    def _compute_patience(self, target_date: date, *, agent_id: str | None = None) -> int:
        """Patience: consistency of run durations (low variance = high patience).

        Uses coefficient of variation (CV = stddev/avg) scaled so:
          CV=0.0 → 100 (perfectly consistent)
          CV=1.0 → 50  (moderate variance — normal for mixed agent types)
          CV>=2.0 → 0  (extremely chaotic)
        """
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
                    # Scale: CV=0→100, CV=1→50, CV>=2→0
                    cv = std_ms / avg_ms if avg_ms > 0 else 1.0
                    score = 100 * (1.0 - min(cv, 2.0) / 2.0)
                    return min(100, max(0, int(score)))
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
        """Wisdom: insights and dreams weighted score.

        Scaled so growth is gradual:
          1 insight + 1 dream = 15
          5 insights + 5 dreams = 75
          7+ insights + 5+ dreams = 100
        """
        raw = stats.insights_generated * 10 + stats.dreams_completed * 5
        # Logarithmic scaling: first points are easy, later points are harder
        if raw <= 0:
            return 0
        # log-scale: ln(raw+1) / ln(150) * 100 → 100 at raw=150
        import math

        score = min(100, int(100 * math.log(raw + 1) / math.log(150)))
        return score

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

    def _compute_effectiveness(self, target_date: date, *, agent_id: str | None = None) -> int:
        """Effectiveness: outcome quality over last 7 days.

        Reads outcome_assessment from agent_runs. Score = satisfaction_rate * 100.
        Only counts runs with non-null outcome_assessment.
        Abandoned runs are excluded (same as analytics.py).
        """
        try:
            from robothor.db.connection import get_connection

            start = target_date - timedelta(days=7)
            ac, ac_params = _agent_clause(agent_id)
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT outcome_assessment, COUNT(*)
                    FROM agent_runs
                    WHERE DATE(started_at AT TIME ZONE 'America/New_York') BETWEEN %s AND %s
                      AND outcome_assessment IS NOT NULL
                    """
                    + ac
                    + """
                    GROUP BY outcome_assessment
                    """,
                    [start, target_date, *ac_params],
                )
                rows = cur.fetchall()
                if not rows:
                    return 50  # No assessed runs — neutral

                outcome_dist = {r[0]: r[1] for r in rows}
                successful = outcome_dist.get("successful", 0)
                unsatisfied = sum(outcome_dist.get(k, 0) for k in ("partial", "incorrect"))
                total_rated = successful + unsatisfied
                if total_rated == 0:
                    return 50  # Only abandoned runs — neutral
                return min(100, max(0, int(100 * successful / total_rated)))
        except Exception:
            pass
        return 50

    def _evaluate_goals(
        self, agent_id: str, goals: list[dict[str, Any]], target_date: date
    ) -> float:
        """Evaluate per-agent goals. Returns 0.0-1.0 (weighted fraction met).

        Supported metrics: completion_rate, avg_duration_ms, satisfaction_rate,
        error_rate, timeout_rate.
        Target operators: <, >, <=, >=, =
        """
        import re

        if not goals:
            return 1.0  # No goals = vacuously met

        total_weight = 0.0
        met_weight = 0.0

        for goal in goals:
            metric = goal.get("metric", "")
            target_str = goal.get("target", "")
            weight = float(goal.get("weight", 1.0))

            # Parse target: operator + value
            match = re.match(r"([<>=!]+)\s*(.+)", target_str)
            if not match:
                continue
            op, val_str = match.group(1), match.group(2)
            try:
                target_val = float(val_str)
            except ValueError:
                continue

            # Get actual metric value
            actual = self._get_goal_metric(agent_id, metric, target_date)
            if actual is None:
                continue  # No data — exclude from scoring (don't penalize)

            # Only count goals we can actually measure
            total_weight += weight

            # Compare
            met = False
            if op == ">":
                met = actual > target_val
            elif op == ">=":
                met = actual >= target_val
            elif op == "<":
                met = actual < target_val
            elif op == "<=":
                met = actual <= target_val
            elif op == "=":
                met = abs(actual - target_val) < 0.001

            if met:
                met_weight += weight

        return met_weight / total_weight if total_weight > 0 else 1.0

    def _get_goal_metric(self, agent_id: str, metric: str, target_date: date) -> float | None:
        """Fetch a metric value for goal evaluation."""
        try:
            from robothor.db.connection import get_connection

            start = target_date - timedelta(days=7)
            with get_connection() as conn:
                cur = conn.cursor()
                if metric == "completion_rate":
                    cur.execute(
                        """
                        SELECT COUNT(*) as total,
                               COUNT(*) FILTER (WHERE status = 'completed') as completed
                        FROM agent_runs
                        WHERE agent_id = %s
                          AND DATE(started_at AT TIME ZONE 'America/New_York') BETWEEN %s AND %s
                        """,
                        (agent_id, start, target_date),
                    )
                    row = cur.fetchone()
                    if row and row[0] > 0:
                        return float(row[1]) / float(row[0])
                elif metric == "avg_duration_ms":
                    cur.execute(
                        """
                        SELECT AVG(duration_ms)
                        FROM agent_runs
                        WHERE agent_id = %s
                          AND DATE(started_at AT TIME ZONE 'America/New_York') BETWEEN %s AND %s
                          AND status = 'completed' AND duration_ms > 0
                        """,
                        (agent_id, start, target_date),
                    )
                    row = cur.fetchone()
                    if row and row[0] is not None:
                        return float(row[0])
                elif metric == "satisfaction_rate":
                    cur.execute(
                        """
                        SELECT outcome_assessment, COUNT(*)
                        FROM agent_runs
                        WHERE agent_id = %s
                          AND DATE(started_at AT TIME ZONE 'America/New_York') BETWEEN %s AND %s
                          AND outcome_assessment IS NOT NULL
                        GROUP BY outcome_assessment
                        """,
                        (agent_id, start, target_date),
                    )
                    dist = {r[0]: r[1] for r in cur.fetchall()}
                    s = dist.get("successful", 0)
                    u = sum(dist.get(k, 0) for k in ("partial", "incorrect"))
                    if s + u > 0:
                        return float(s) / float(s + u)
                elif metric == "error_rate":
                    cur.execute(
                        """
                        SELECT COUNT(*) as total,
                               COUNT(*) FILTER (WHERE status IN ('failed', 'timeout')) as errors
                        FROM agent_runs
                        WHERE agent_id = %s
                          AND DATE(started_at AT TIME ZONE 'America/New_York') BETWEEN %s AND %s
                        """,
                        (agent_id, start, target_date),
                    )
                    row = cur.fetchone()
                    if row and row[0] > 0:
                        return float(row[1]) / float(row[0])
                elif metric == "timeout_rate":
                    cur.execute(
                        """
                        SELECT COUNT(*) as total,
                               COUNT(*) FILTER (WHERE status = 'timeout') as timeouts
                        FROM agent_runs
                        WHERE agent_id = %s
                          AND DATE(started_at AT TIME ZONE 'America/New_York') BETWEEN %s AND %s
                        """,
                        (agent_id, start, target_date),
                    )
                    row = cur.fetchone()
                    if row and row[0] > 0:
                        return float(row[1]) / float(row[0])
        except Exception:
            pass
        return None

    def _compute_benchmark_score(self, agent_id: str) -> int:
        """Benchmark dimension: latest benchmark aggregate score (0-100).

        Reads from the memory block written by benchmark_run tool.
        Returns 50 (neutral) if no benchmark data exists.
        """
        try:
            score, _ = self._get_latest_benchmark(agent_id)
            if score is not None:
                return min(100, max(0, int(score * 100)))
        except Exception:
            pass
        return 50

    def _compute_review_score(self, agent_id: str) -> int | None:
        """Review score from formal feedback (0-100), or None if no reviews.

        Reads from agent_reviews table (last 30 days). Scales 1-5 rating
        to 0-100: rating 1 → 0, rating 3 → 50, rating 5 → 100.
        """
        try:
            from robothor.crm.dal import get_review_summary

            summary = get_review_summary(agent_id, days=30)
            if not summary or summary["count"] == 0:
                return None
            avg = summary["avg_rating"]
            if avg is None:
                return None
            return min(100, max(0, int((avg - 1) * 25)))
        except Exception:
            return None

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
        # Compute new dimensions for per-agent scoring
        benchmark_dim = self._compute_benchmark_score(agent_id)
        stats.benchmark_dim_score = benchmark_dim
        # Blend goal attainment into effectiveness if agent has goals defined
        try:
            from pathlib import Path

            from robothor.engine.config import load_agent_config

            manifest_dir = Path(
                __import__("os").environ.get(
                    "ROBOTHOR_MANIFEST_DIR",
                    str(Path.home() / "robothor" / "docs" / "agents"),
                )
            )
            agent_config = load_agent_config(agent_id, manifest_dir)
            goals = agent_config.goals if agent_config else None
            goal_score = self._evaluate_goals(agent_id, goals, target_date) if goals else None
            review_score = self._compute_review_score(agent_id)
            outcome_raw = stats.effectiveness_score

            # Blend effectiveness from available signals
            if goal_score is not None and review_score is not None:
                # All three signals: 40% outcome + 30% goals + 30% reviews
                stats.effectiveness_score = min(
                    100,
                    max(0, int(outcome_raw * 0.4 + goal_score * 100 * 0.3 + review_score * 0.3)),
                )
            elif goal_score is not None:
                # Outcome + goals: 50/50
                stats.effectiveness_score = min(
                    100, max(0, int(outcome_raw * 0.5 + goal_score * 100 * 0.5))
                )
            elif review_score is not None:
                # Outcome + reviews: 60/40
                stats.effectiveness_score = min(
                    100, max(0, int(outcome_raw * 0.6 + review_score * 0.4))
                )
            # else: outcome only (already set)
        except Exception:
            pass  # Goal/review evaluation is best-effort; never block scoring
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
            effectiveness_score=stats.effectiveness_score,
            benchmark_dim_score=benchmark_dim,
            reliability_score=stats.reliability_score,
            overall_score=compute_overall_score(
                stats.debugging_score,
                stats.patience_score,
                stats.effectiveness_score,
                benchmark_dim,
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
                        wisdom_score, effectiveness_score, benchmark_dim_score,
                        reliability_score, overall_score,
                        daily_xp, total_xp, level,
                        last_benchmark_score, last_benchmark_at, computed_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()
                    )
                    ON CONFLICT (agent_id, stat_date) DO UPDATE SET
                        tasks_completed = EXCLUDED.tasks_completed,
                        errors_recovered = EXCLUDED.errors_recovered,
                        debugging_score = EXCLUDED.debugging_score,
                        patience_score = EXCLUDED.patience_score,
                        chaos_score = EXCLUDED.chaos_score,
                        wisdom_score = EXCLUDED.wisdom_score,
                        effectiveness_score = EXCLUDED.effectiveness_score,
                        benchmark_dim_score = EXCLUDED.benchmark_dim_score,
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
                        stats.effectiveness_score,
                        stats.benchmark_dim_score,
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

    ESCALATION_FLAG_THRESHOLD = 3  # flag count at which we escalate to auto-researcher

    def flag_underperformers(self, threshold: int = 70, consecutive_days: int = 2) -> list[str]:
        """Flag agents with consistently low scores for optimization.

        First flags go to auto-agent. After ESCALATION_FLAG_THRESHOLD
        consecutive flags, escalate to auto-researcher for deeper analysis.

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

            for aid in candidates:
                count = self._get_flag_count(aid)
                if count >= self.ESCALATION_FLAG_THRESHOLD:
                    if self._create_researcher_task(aid, threshold):
                        flagged.append(aid)
                else:
                    if self._create_autoagent_task(aid, threshold):
                        flagged.append(aid)

        except Exception as e:
            logger.warning("Failed to flag underperformers: %s", e)

        return flagged

    def _get_flag_count(self, agent_id: str) -> int:
        """Count how many times this agent has been flagged (open + resolved tasks)."""
        try:
            from robothor.crm.dal import list_tasks

            existing = list_tasks(
                tags=["low-score", agent_id],
            )
            if isinstance(existing, list):
                return len(existing)
        except Exception:
            logger.debug("Failed to get flag count for %s", agent_id, exc_info=True)
        return 0

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
                               effectiveness_score, benchmark_dim_score, reliability_score
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
                            f"Effectiveness: {row[3]}, Benchmark: {row[4]}, Reliability: {row[5]}"
                        )
            except Exception:
                pass

            # Get recent review feedback for context
            review_context = ""
            try:
                from robothor.crm.dal import get_review_summary

                review_summary = get_review_summary(agent_id, days=30)
                if review_summary and review_summary["count"] > 0:
                    review_context = (
                        f"\n\n**Recent reviews:** {review_summary['count']} reviews, "
                        f"avg rating: {review_summary['avg_rating']}/5\n"
                    )
                    for fb in review_summary.get("recent_feedback", [])[:3]:
                        review_context += f"- {fb.get('feedback', '')}\n"
            except Exception:
                pass

            create_task(
                title=f"Optimize {agent_id}: low score (below {threshold})",
                body=(
                    f"Agent {agent_id} has scored below {threshold} for 2+ consecutive days.\n\n"
                    f"**Current scores:** {scores_str}\n\n"
                    f"Define a benchmark suite (if none exists), run it, and iterate on "
                    f"the agent's instruction file and manifest to improve performance."
                    f"{review_context}"
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

    def _create_researcher_task(self, agent_id: str, threshold: int) -> bool:
        """Escalate a repeat underperformer to auto-researcher for deeper analysis."""
        try:
            from robothor.crm.dal import create_task, list_tasks

            # Check for existing open researcher task for this agent
            existing = list_tasks(
                assigned_to_agent="auto-researcher",
                status="TODO",
                tags=[agent_id],
            )
            if existing and not isinstance(existing, dict):
                return False

            # Get latest scores for context
            scores_str = ""
            try:
                from robothor.db.connection import get_connection

                with get_connection() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT overall_score, debugging_score, patience_score,
                               effectiveness_score, benchmark_dim_score, reliability_score
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
                            f"Effectiveness: {row[3]}, Benchmark: {row[4]}, Reliability: {row[5]}"
                        )
            except Exception:
                pass

            # Get recent review feedback for context
            review_context = ""
            try:
                from robothor.crm.dal import get_review_summary

                review_summary = get_review_summary(agent_id, days=30)
                if review_summary and review_summary["count"] > 0:
                    review_context = (
                        f"\n\n**Recent reviews:** {review_summary['count']} reviews, "
                        f"avg rating: {review_summary['avg_rating']}/5\n"
                    )
                    for fb in review_summary.get("recent_feedback", [])[:3]:
                        review_context += f"- {fb.get('feedback', '')}\n"
            except Exception:
                pass

            create_task(
                title=f"Deep analysis {agent_id}: persistent low score (below {threshold})",
                body=(
                    f"Agent {agent_id} has been flagged {self.ESCALATION_FLAG_THRESHOLD}+ times "
                    f"and auto-agent optimization has not resolved the issue.\n\n"
                    f"**Current scores:** {scores_str}\n\n"
                    f"Run a controlled experiment to identify the root cause. "
                    f"Consider: prompt structure, model fit, tool selection, "
                    f"task complexity mismatch."
                    f"{review_context}"
                ),
                assigned_to_agent="auto-researcher",
                tags=["auto-researcher", "low-score", agent_id],
                priority="high",
            )
            logger.info(
                "Escalated %s to auto-researcher (flagged %d+ times)",
                agent_id,
                self.ESCALATION_FLAG_THRESHOLD,
            )
            return True

        except Exception as e:
            logger.warning("Failed to create researcher task for %s: %s", agent_id, e)
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
                        wisdom_score, effectiveness_score, benchmark_dim_score,
                        reliability_score,
                        total_xp, level, current_streak_days, longest_streak_days
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        effectiveness_score = EXCLUDED.effectiveness_score,
                        benchmark_dim_score = EXCLUDED.benchmark_dim_score,
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
                        stats.effectiveness_score,
                        stats.benchmark_dim_score,
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

        # ── Write automated system reviews for notable score changes ────────
        try:
            if fleet:
                reviews_written = self._write_system_reviews(fleet)
                if reviews_written:
                    result["system_reviews"] = reviews_written
        except Exception as e:
            logger.debug("Failed to write system reviews: %s", e)

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

    def _write_system_reviews(self, fleet: list[AgentBuddyStats]) -> int:
        """Generate system reviews for agents with notable scores.

        Writes a review when:
        - overall_score < 40: rating 1 (critical)
        - overall_score < 60: rating 2 (concern)
        - overall_score >= 90: rating 5 (excellent)

        Returns count of reviews written.
        """
        from robothor.crm.dal import create_review, get_reviews

        count = 0
        for stats in fleet:
            if stats.overall_score < 40:
                rating, label = 1, "critical"
            elif stats.overall_score < 60:
                rating, label = 2, "concern"
            elif stats.overall_score >= 90:
                rating, label = 5, "excellent"
            else:
                continue  # 60-89 is normal — no review needed

            # Dedup: skip if a system review already exists for this agent today
            try:
                existing = get_reviews(stats.agent_id, days=1, reviewer_type="system")
                if existing:
                    continue
            except Exception:
                pass  # If check fails, write the review anyway

            create_review(
                agent_id=stats.agent_id,
                reviewer="buddy",
                reviewer_type="system",
                rating=rating,
                feedback=(
                    f"[{label}] Overall: {stats.overall_score}, "
                    f"Eff: {stats.effectiveness_score}, "
                    f"Bench: {stats.benchmark_dim_score}, "
                    f"R: {stats.reliability_score}, "
                    f"D: {stats.debugging_score}, "
                    f"P: {stats.patience_score}"
                ),
            )
            count += 1
        return count

    def _update_status_block(
        self, stats: DailyStats, daily_xp: int, streak: int, level: LevelInfo
    ) -> None:
        """Write buddy_status memory block for warmup injection."""
        try:
            from robothor.memory.blocks import write_block

            lines = [
                f"Level {level.level} {level.level_name} ({level.total_xp + daily_xp:,} XP) | {streak}-day streak",
                f"Debugging: {stats.debugging_score} | Patience: {stats.patience_score} | "
                f"Effectiveness: {stats.effectiveness_score} | Benchmark: {stats.benchmark_dim_score} | "
                f"Reliability: {stats.reliability_score}",
                f"Today: {stats.tasks_completed} tasks, {stats.emails_processed} emails, "
                f"{stats.insights_generated} insights, {stats.dreams_completed} dreams (+{daily_xp} XP)",
            ]
            write_block("buddy_status", "\n".join(lines))
        except Exception as e:
            logger.debug("Failed to update buddy_status block: %s", e)

    # ── Heartbeat context (for buddy reflection) ──────────────────────────

    _STREAK_MILESTONES = frozenset({7, 14, 30, 60, 100})

    # Cooldown durations (hours) — events won't re-fire within this window.
    _EVENT_COOLDOWNS: dict[str, int] = {
        "level_up": 24,
        "streak_milestone": 24,
        "score_threshold_up": 12,
        "score_threshold_down": 12,
        "score_swing": 4,
    }

    _COOLDOWN_BLOCK = "buddy_event_cooldowns"

    def _load_event_cooldowns(self) -> dict[str, str]:
        """Load event cooldown timestamps from memory block."""
        try:
            from robothor.memory.blocks import read_block

            result = read_block(self._COOLDOWN_BLOCK)
            content = result.get("content", "")
            if content and isinstance(content, str):
                return dict(json.loads(content))
        except Exception:
            pass
        return {}

    def _save_event_cooldowns(self, state: dict[str, str]) -> None:
        """Persist event cooldown timestamps to memory block."""
        try:
            from robothor.memory.blocks import write_block

            write_block(self._COOLDOWN_BLOCK, json.dumps(state))
        except Exception as e:
            logger.debug("Failed to save buddy event cooldowns: %s", e)

    def _is_on_cooldown(self, state: dict[str, str], event_key: str) -> bool:
        """Check if an event is still within its cooldown window."""
        last_fired_str = state.get(f"cooldown_{event_key}")
        if not last_fired_str:
            return False
        try:
            last_fired = datetime.fromisoformat(last_fired_str)
            cooldown_hours = self._EVENT_COOLDOWNS.get(event_key, 4)
            return datetime.now(UTC) - last_fired < timedelta(hours=cooldown_hours)
        except Exception:
            return False

    def _mark_event_fired(self, state: dict[str, str], event_key: str) -> None:
        """Record that an event just fired."""
        state[f"cooldown_{event_key}"] = datetime.now(UTC).isoformat()

    def _get_status_and_deltas(self, target_date: date | None = None) -> dict[str, Any]:
        """Compute current status: level, streak, scores, deltas, fleet rankings.

        Shared helper for both get_buddy_status() and get_buddy_events().
        """
        if target_date is None:
            target_date = datetime.now(UTC).date()

        level_info = self.get_level_info()
        streak = self.get_streak(target_date)
        scores_today = self.compute_daily_stats(target_date)

        # ── Yesterday's scores for delta computation ──
        score_deltas: dict[str, int] = {}
        yesterday_level: int | None = None
        try:
            from robothor.db.connection import get_connection

            yesterday = target_date - timedelta(days=1)
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT debugging_score, patience_score, effectiveness_score,
                           benchmark_dim_score, reliability_score
                    FROM buddy_stats WHERE stat_date = %s
                    """,
                    (yesterday,),
                )
                row = cur.fetchone()
                if row:
                    score_deltas = {
                        "debugging": scores_today.debugging_score - row[0],
                        "patience": scores_today.patience_score - row[1],
                        "effectiveness": scores_today.effectiveness_score - row[2],
                        "benchmark": scores_today.benchmark_dim_score - row[3],
                        "reliability": scores_today.reliability_score - row[4],
                    }

                cur.execute(
                    "SELECT level FROM buddy_stats WHERE stat_date = %s",
                    (yesterday,),
                )
                lrow = cur.fetchone()
                if lrow:
                    yesterday_level = int(lrow[0])
        except Exception:
            pass

        # ── Fleet rankings ──
        fleet_top: list[dict[str, Any]] = []
        try:
            fleet = self.compute_fleet_scores(target_date)
            fleet_top.extend(
                {"agent_id": a.agent_id, "overall_score": a.overall_score, "rank": a.rank}
                for a in fleet[:3]
            )
        except Exception:
            pass

        overall = compute_overall_score(
            scores_today.debugging_score,
            scores_today.patience_score,
            scores_today.effectiveness_score,
            scores_today.benchmark_dim_score,
            scores_today.reliability_score,
        )

        return {
            "level_info": level_info,
            "streak": streak,
            "scores_today": scores_today,
            "score_deltas": score_deltas,
            "yesterday_level": yesterday_level,
            "fleet_top": fleet_top,
            "overall_score": overall,
        }

    def get_buddy_status(self, target_date: date | None = None) -> dict[str, Any]:
        """Current fleet status — level, streak, scores, deltas, fleet rankings.

        Safe to call on every heartbeat. No events, no side effects.
        Used by warmup context injection.
        """
        ctx = self._get_status_and_deltas(target_date)
        # Strip internal-only fields
        ctx.pop("yesterday_level", None)
        return ctx

    def get_buddy_events(self, target_date: date | None = None) -> dict[str, Any]:
        """Detect notable events, gated by cooldowns.

        Each event fires at most once within its cooldown window.
        Calling this method consumes eligible events (marks their cooldowns).
        Used by delivery reflection path.

        Returns the full context dict with an 'events' list. If no events
        pass the cooldown filter, the list is empty.
        """
        ctx = self._get_status_and_deltas(target_date)
        cooldown_state = self._load_event_cooldowns()
        events: list[str] = []
        fired_any = False

        level_info = ctx["level_info"]
        streak = ctx["streak"]
        scores_today = ctx["scores_today"]
        score_deltas = ctx["score_deltas"]
        yesterday_level = ctx.pop("yesterday_level", None)
        overall = ctx["overall_score"]

        # Streak milestones
        current_streak = streak[0]
        if current_streak in self._STREAK_MILESTONES and not self._is_on_cooldown(
            cooldown_state, "streak_milestone"
        ):
            events.append(f"{current_streak}-day streak milestone!")
            self._mark_event_fired(cooldown_state, "streak_milestone")
            fired_any = True

        # Level-up
        if (
            yesterday_level is not None
            and level_info.level > yesterday_level
            and not self._is_on_cooldown(cooldown_state, "level_up")
        ):
            events.append(
                f"Level up! {yesterday_level} → {level_info.level} ({level_info.level_name})"
            )
            self._mark_event_fired(cooldown_state, "level_up")
            fired_any = True

        # Score threshold crossings
        if score_deltas:
            yesterday_overall = compute_overall_score(
                scores_today.debugging_score - score_deltas.get("debugging", 0),
                scores_today.patience_score - score_deltas.get("patience", 0),
                scores_today.effectiveness_score - score_deltas.get("effectiveness", 0),
                scores_today.benchmark_dim_score - score_deltas.get("benchmark", 0),
                scores_today.reliability_score - score_deltas.get("reliability", 0),
            )
            if (
                overall >= 85
                and yesterday_overall < 85
                and not self._is_on_cooldown(cooldown_state, "score_threshold_up")
            ):
                events.append("Fleet overall crossed above 85 — thriving!")
                self._mark_event_fired(cooldown_state, "score_threshold_up")
                fired_any = True
            elif (
                overall < 50
                and yesterday_overall >= 50
                and not self._is_on_cooldown(cooldown_state, "score_threshold_down")
            ):
                events.append("Fleet overall dropped below 50 — needs attention")
                self._mark_event_fired(cooldown_state, "score_threshold_down")
                fired_any = True

            # Large score swings (> 10 pts in any dimension)
            for dim, delta in score_deltas.items():
                if abs(delta) > 10:
                    swing_key = f"score_swing_{dim}"
                    if not self._is_on_cooldown(cooldown_state, swing_key):
                        direction = "up" if delta > 0 else "down"
                        events.append(f"{dim.capitalize()} {direction} {abs(delta)} pts")
                        self._mark_event_fired(cooldown_state, swing_key)
                        fired_any = True

        # Persist cooldown state if any events fired
        if fired_any:
            self._save_event_cooldowns(cooldown_state)

        ctx["events"] = events
        return ctx

    def get_buddy_heartbeat_context(self, target_date: date | None = None) -> dict[str, Any]:
        """Legacy wrapper — returns status with cooldown-gated events.

        Callers should prefer get_buddy_status() (warmup) or
        get_buddy_events() (delivery) directly.
        """
        return self.get_buddy_events(target_date)

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
