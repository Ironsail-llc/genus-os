"""Buddy — fleet scoring against declared goals.

Source of truth is `robothor/engine/goals.py`. Each agent's score is the
weighted fraction of its declared goals it is satisfying right now, scaled
to 0-100. The operator's mental model — "percentages per stat that go up as
the agent hits its goals" — is already the shape `compute_achievement_score`
returns. This module persists those scores daily and exposes a minimal API
for the health endpoints, Telegram display surfaces, and daily refresh.

Legacy RPG scoring (XP/levels/streaks, debugging/patience/chaos/wisdom
dimensions) was removed on 2026-04-18. The raw legacy columns stay in the
DB for a 30-day soak and are dropped by migration 035.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from robothor.constants import DEFAULT_TENANT
from robothor.engine.goals import compute_achievement_score, parse_goals_from_manifest

logger = logging.getLogger(__name__)


AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "agents"


@dataclass
class AgentScore:
    """One agent's achievement snapshot for a given day."""

    agent_id: str
    achievement_score: int  # 0-100
    rating: int  # 1-5
    satisfied_goals: int
    breached_goals: int
    stat_date: date
    rank: int = 0

    @property
    def overall_score(self) -> int:
        """Back-compat alias used by older surfaces. Same value as achievement_score."""
        return self.achievement_score


@dataclass
class FleetStatus:
    """Fleet-level snapshot for a given day."""

    stat_date: date
    fleet_achievement_score: int  # 0-100, mean of per-agent scores
    tasks_completed: int  # total completed runs in the trailing 24h (cosmetic)
    per_agent: list[AgentScore] = field(default_factory=list)


def _load_manifests() -> list[tuple[str, dict[str, Any]]]:
    """Return (agent_id, manifest_dict) for every YAML under docs/agents/."""
    out: list[tuple[str, dict[str, Any]]] = []
    if not AGENTS_DIR.is_dir():
        return out
    for path in sorted(AGENTS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            logger.warning("Skipping manifest %s: %s", path.name, e)
            continue
        agent_id = data.get("id")
        if agent_id:
            out.append((str(agent_id), data))
    return out


class BuddyEngine:
    """Thin facade over goals.py's compute_achievement_score.

    Everything is read-on-demand from the current run history — no shadow
    bookkeeping. `refresh_daily()` snapshots today's scores into
    agent_buddy_stats.achievement_score (and buddy_stats for the fleet avg)
    for historical comparison and rolling-window queries.
    """

    def __init__(self, tenant_id: str = DEFAULT_TENANT) -> None:
        self.tenant_id = tenant_id

    def compute_agent_score(
        self,
        agent_id: str,
        manifest: dict[str, Any] | None = None,
        target_date: date | None = None,
    ) -> AgentScore:
        """Achievement score for a single agent. Loads the manifest if not supplied."""
        if manifest is None:
            for aid, data in _load_manifests():
                if aid == agent_id:
                    manifest = data
                    break
        if manifest is None:
            return AgentScore(
                agent_id=agent_id,
                achievement_score=0,
                rating=1,
                satisfied_goals=0,
                breached_goals=0,
                stat_date=target_date or datetime.now(UTC).date(),
            )

        goals = parse_goals_from_manifest(manifest)
        if not goals:
            return AgentScore(
                agent_id=agent_id,
                achievement_score=0,
                rating=1,
                satisfied_goals=0,
                breached_goals=0,
                stat_date=target_date or datetime.now(UTC).date(),
            )

        result = compute_achievement_score(agent_id, goals, tenant_id=self.tenant_id)
        return AgentScore(
            agent_id=agent_id,
            achievement_score=int(round(float(result["score"]) * 100)),
            rating=int(result["rating"]),
            satisfied_goals=len(result.get("satisfied_goals", [])),
            breached_goals=len(result.get("breached_goals", [])),
            stat_date=target_date or datetime.now(UTC).date(),
        )

    def compute_fleet_scores(self, target_date: date | None = None) -> list[AgentScore]:
        """Rank all agents by achievement score (desc). Agents with no goals are omitted."""
        scores: list[AgentScore] = []
        for agent_id, manifest in _load_manifests():
            if not parse_goals_from_manifest(manifest):
                continue
            scores.append(self.compute_agent_score(agent_id, manifest, target_date))
        scores.sort(key=lambda s: (-s.achievement_score, s.agent_id))
        for i, s in enumerate(scores, start=1):
            s.rank = i
        return scores

    def compute_daily_stats(
        self, target_date: date | None = None, *, agent_id: str | None = None
    ) -> FleetStatus:
        """Back-compat entry: returns a FleetStatus (or single-agent slice)."""
        today = target_date or datetime.now(UTC).date()
        if agent_id is not None:
            one = self.compute_agent_score(agent_id, target_date=today)
            return FleetStatus(
                stat_date=today,
                fleet_achievement_score=one.achievement_score,
                tasks_completed=self._tasks_completed_24h(agent_id),
                per_agent=[one],
            )
        scores = self.compute_fleet_scores(today)
        fleet_avg = (
            int(round(sum(s.achievement_score for s in scores) / len(scores))) if scores else 0
        )
        return FleetStatus(
            stat_date=today,
            fleet_achievement_score=fleet_avg,
            tasks_completed=self._tasks_completed_24h(None),
            per_agent=scores,
        )

    def refresh_daily(self) -> dict[str, Any]:
        """Persist today's achievement scores into buddy_stats + agent_buddy_stats.

        Called from the evening-winddown / autodream deep-mode cron. Idempotent:
        running twice on the same day overwrites the earlier row.
        """
        today = datetime.now(UTC).date()
        scores = self.compute_fleet_scores(today)
        fleet_avg = (
            int(round(sum(s.achievement_score for s in scores) / len(scores))) if scores else 0
        )

        from robothor.db.connection import get_connection

        agent_rows = 0
        with get_connection() as conn, conn.cursor() as cur:
            for s in scores:
                cur.execute(
                    """
                    INSERT INTO agent_buddy_stats (agent_id, stat_date, achievement_score)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (agent_id, stat_date)
                    DO UPDATE SET achievement_score = EXCLUDED.achievement_score,
                                  computed_at = NOW()
                    """,
                    (s.agent_id, today, s.achievement_score),
                )
                agent_rows += 1
            cur.execute(
                """
                INSERT INTO buddy_stats (stat_date, achievement_score)
                VALUES (%s, %s)
                ON CONFLICT (stat_date)
                DO UPDATE SET achievement_score = EXCLUDED.achievement_score
                """,
                (today, fleet_avg),
            )
            conn.commit()

        return {
            "stat_date": today.isoformat(),
            "fleet_achievement_score": fleet_avg,
            "agents_scored": agent_rows,
        }

    def increment_task_count(self, agent_id: str | None = None) -> None:
        """Lightweight counter increment — called by AGENT_END lifecycle hook.

        Only updates tasks_completed on today's buddy_stats / agent_buddy_stats
        row. Does NOT recompute achievement scores. Kept as a compatibility
        shim; the same information is available from agent_runs directly.
        """
        try:
            from robothor.db.connection import get_connection

            today = datetime.now(UTC).date()
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO buddy_stats (stat_date, tasks_completed)
                    VALUES (%s, 1)
                    ON CONFLICT (stat_date)
                    DO UPDATE SET tasks_completed = COALESCE(buddy_stats.tasks_completed, 0) + 1
                    """,
                    (today,),
                )
                if agent_id:
                    cur.execute(
                        """
                        INSERT INTO agent_buddy_stats (agent_id, stat_date, tasks_completed)
                        VALUES (%s, %s, 1)
                        ON CONFLICT (agent_id, stat_date)
                        DO UPDATE SET tasks_completed =
                            COALESCE(agent_buddy_stats.tasks_completed, 0) + 1
                        """,
                        (agent_id, today),
                    )
                conn.commit()
        except Exception as e:
            logger.debug("increment_task_count failed (non-fatal): %s", e)

    def get_streak(self, target_date: date | None = None) -> tuple[int, int]:
        """(current_streak_days, longest_streak_days) computed from buddy_stats.

        A streak day = a day where tasks_completed > 0. Kept cosmetic only —
        never feeds the achievement score.
        """
        today = target_date or datetime.now(UTC).date()
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT stat_date, COALESCE(tasks_completed, 0)
                    FROM buddy_stats
                    WHERE stat_date <= %s
                    ORDER BY stat_date DESC
                    LIMIT 365
                    """,
                    (today,),
                )
                rows = cur.fetchall()
        except Exception as e:
            logger.debug("get_streak failed: %s", e)
            return (0, 0)

        current = 0
        expected = today
        for stat_date, tasks in rows:
            if stat_date == expected and tasks > 0:
                current += 1
                expected = expected - timedelta(days=1)
            else:
                break

        longest = current
        run = 0
        prev: date | None = None
        for stat_date, tasks in rows:
            if tasks <= 0:
                run = 0
                prev = None
                continue
            if prev is None or (prev - stat_date).days == 1:
                run += 1
                longest = max(longest, run)
            else:
                run = 1
            prev = stat_date
        return (current, longest)

    def _tasks_completed_24h(self, agent_id: str | None) -> int:
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn, conn.cursor() as cur:
                if agent_id:
                    cur.execute(
                        """
                        SELECT COUNT(*) FROM agent_runs
                        WHERE agent_id = %s
                          AND parent_run_id IS NULL
                          AND started_at > NOW() - INTERVAL '24 hours'
                          AND status = 'completed'
                        """,
                        (agent_id,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT COUNT(*) FROM agent_runs
                        WHERE parent_run_id IS NULL
                          AND started_at > NOW() - INTERVAL '24 hours'
                          AND status = 'completed'
                        """
                    )
                row = cur.fetchone()
                return int(row[0] or 0) if row else 0
        except Exception:
            return 0
