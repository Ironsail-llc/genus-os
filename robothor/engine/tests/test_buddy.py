"""Tests for the slim goals-backed Buddy engine.

The legacy RPG scoring tests (XP/levels/streaks, debugging/patience/chaos/
wisdom dimensions, flag_underperformers escalation, event cooldowns) were
removed on 2026-04-18 when that code went away. Scoring now comes from
`robothor/engine/goals.py`; tests for the metric math live in
`test_goals.py`. This file covers the thin BuddyEngine facade.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch


class TestWarmupHasNoBuddyHook:
    """The old _buddy_status_context hook must stay deleted — heartbeat is untouched."""

    def test_warmup_hook_deleted(self):
        from robothor.engine import warmup

        assert not hasattr(warmup, "_buddy_status_context"), (
            "_buddy_status_context was resurrected — Buddy must not inject into warmup"
        )


class TestLegacyRPGSymbolsRemoved:
    """Regression check: the RPG gamification API stays out of the codebase."""

    def test_legacy_functions_removed(self):
        from robothor.engine import buddy

        for name in (
            "xp_for_level",
            "level_from_xp",
            "level_name",
            "compute_overall_score",
            "LevelInfo",
            "DailyStats",
            "AgentBuddyStats",
        ):
            assert not hasattr(buddy, name), (
                f"Legacy RPG symbol {name} resurrected — goals.py is the source of truth"
            )

    def test_legacy_engine_methods_removed(self):
        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        for name in (
            "get_level_info",
            "flag_underperformers",
            "get_buddy_events",
            "get_buddy_heartbeat_context",
        ):
            assert not hasattr(engine, name), (
                f"BuddyEngine.{name} resurrected — legacy RPG scoring stays deleted"
            )


class TestAgentScore:
    def test_overall_score_alias(self):
        from robothor.engine.buddy import AgentScore

        s = AgentScore(
            agent_id="x",
            achievement_score=72,
            rating=3,
            satisfied_goals=4,
            breached_goals=1,
            stat_date=date(2026, 4, 18),
        )
        assert s.overall_score == 72  # back-compat alias


class TestComputeAgentScore:
    @patch("robothor.engine.buddy.compute_achievement_score")
    @patch("robothor.engine.buddy.parse_goals_from_manifest")
    def test_scales_score_to_0_100(self, mock_parse, mock_compute):
        from robothor.engine.buddy import BuddyEngine

        mock_parse.return_value = [object()]  # one goal
        mock_compute.return_value = {
            "score": 0.8234,
            "rating": 4,
            "satisfied_goals": ["g1", "g2"],
            "breached_goals": ["g3"],
            "per_goal": [],
        }
        result = BuddyEngine().compute_agent_score(
            "test-agent",
            manifest={"id": "test-agent", "goals": {"correctness": [{"id": "g"}]}},
            target_date=date(2026, 4, 18),
        )
        assert result.agent_id == "test-agent"
        assert result.achievement_score == 82  # 0.8234 → 82
        assert result.rating == 4
        assert result.satisfied_goals == 2
        assert result.breached_goals == 1
        assert result.stat_date == date(2026, 4, 18)

    @patch("robothor.engine.buddy.parse_goals_from_manifest")
    def test_no_goals_returns_zero(self, mock_parse):
        from robothor.engine.buddy import BuddyEngine

        mock_parse.return_value = []
        result = BuddyEngine().compute_agent_score(
            "no-goals-agent",
            manifest={"id": "no-goals-agent"},
        )
        assert result.achievement_score == 0
        assert result.rating == 1
        assert result.satisfied_goals == 0
        assert result.breached_goals == 0

    @patch("robothor.engine.buddy._load_manifests")
    def test_missing_manifest_returns_zero(self, mock_load):
        from robothor.engine.buddy import BuddyEngine

        mock_load.return_value = []  # no manifest found for this agent
        result = BuddyEngine().compute_agent_score("unknown-agent")
        assert result.achievement_score == 0


class TestComputeFleetScores:
    @patch("robothor.engine.buddy.compute_achievement_score")
    @patch("robothor.engine.buddy.parse_goals_from_manifest")
    @patch("robothor.engine.buddy._load_manifests")
    def test_ranks_by_score_descending(self, mock_load, mock_parse, mock_compute):
        from robothor.engine.buddy import BuddyEngine

        mock_load.return_value = [
            ("low", {"id": "low"}),
            ("high", {"id": "high"}),
            ("mid", {"id": "mid"}),
        ]
        mock_parse.side_effect = lambda m: [object()]  # each agent has a goal

        def fake_compute(agent_id, goals, tenant_id=None):
            scores = {"low": 0.2, "mid": 0.5, "high": 0.9}
            return {
                "score": scores[agent_id],
                "rating": 3,
                "satisfied_goals": [],
                "breached_goals": [],
                "per_goal": [],
            }

        mock_compute.side_effect = fake_compute

        scores = BuddyEngine().compute_fleet_scores()
        assert [s.agent_id for s in scores] == ["high", "mid", "low"]
        assert [s.rank for s in scores] == [1, 2, 3]
        assert [s.achievement_score for s in scores] == [90, 50, 20]

    @patch("robothor.engine.buddy.parse_goals_from_manifest")
    @patch("robothor.engine.buddy._load_manifests")
    def test_agents_without_goals_excluded(self, mock_load, mock_parse):
        from robothor.engine.buddy import BuddyEngine

        mock_load.return_value = [("no-goals", {"id": "no-goals"})]
        mock_parse.return_value = []  # agent has no declared goals

        scores = BuddyEngine().compute_fleet_scores()
        assert scores == []


class TestComputeDailyStats:
    @patch("robothor.engine.buddy.BuddyEngine._tasks_completed_24h", return_value=42)
    @patch("robothor.engine.buddy.compute_achievement_score")
    @patch("robothor.engine.buddy.parse_goals_from_manifest")
    @patch("robothor.engine.buddy._load_manifests")
    def test_fleet_average(self, mock_load, mock_parse, mock_compute, _mock_tasks):
        from robothor.engine.buddy import BuddyEngine

        mock_load.return_value = [("a", {"id": "a"}), ("b", {"id": "b"})]
        mock_parse.return_value = [object()]
        mock_compute.side_effect = [
            {
                "score": 0.6,
                "rating": 3,
                "satisfied_goals": [],
                "breached_goals": [],
                "per_goal": [],
            },
            {
                "score": 0.8,
                "rating": 4,
                "satisfied_goals": [],
                "breached_goals": [],
                "per_goal": [],
            },
        ]
        fleet = BuddyEngine().compute_daily_stats()
        assert fleet.fleet_achievement_score == 70  # (60 + 80) / 2
        assert fleet.tasks_completed == 42
        assert len(fleet.per_agent) == 2

    @patch("robothor.engine.buddy.BuddyEngine._tasks_completed_24h", return_value=5)
    @patch("robothor.engine.buddy.compute_achievement_score")
    @patch("robothor.engine.buddy.parse_goals_from_manifest")
    @patch("robothor.engine.buddy._load_manifests")
    def test_single_agent_slice(self, mock_load, mock_parse, mock_compute, _mock_tasks):
        from robothor.engine.buddy import BuddyEngine

        mock_load.return_value = [("solo", {"id": "solo"})]
        mock_parse.return_value = [object()]
        mock_compute.return_value = {
            "score": 0.75,
            "rating": 3,
            "satisfied_goals": [],
            "breached_goals": [],
            "per_goal": [],
        }
        fleet = BuddyEngine().compute_daily_stats(agent_id="solo")
        assert fleet.fleet_achievement_score == 75
        assert len(fleet.per_agent) == 1
        assert fleet.per_agent[0].agent_id == "solo"


class TestIncrementTaskCount:
    @patch("robothor.db.connection.get_connection")
    def test_both_tables_touched_when_agent_supplied(self, mock_get_conn):
        from robothor.engine.buddy import BuddyEngine

        cur = mock_get_conn.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        BuddyEngine().increment_task_count(agent_id="my-agent")

        executed_sqls = [call.args[0] for call in cur.execute.call_args_list]
        assert any("INSERT INTO buddy_stats" in s for s in executed_sqls)
        assert any("INSERT INTO agent_buddy_stats" in s for s in executed_sqls)

    @patch("robothor.db.connection.get_connection")
    def test_global_only_when_no_agent(self, mock_get_conn):
        from robothor.engine.buddy import BuddyEngine

        cur = mock_get_conn.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        BuddyEngine().increment_task_count(agent_id=None)
        executed_sqls = [call.args[0] for call in cur.execute.call_args_list]
        assert any("INSERT INTO buddy_stats" in s for s in executed_sqls)
        assert not any("INSERT INTO agent_buddy_stats" in s for s in executed_sqls)

    @patch("robothor.db.connection.get_connection", side_effect=RuntimeError("db down"))
    def test_db_failure_is_swallowed(self, _mock):
        from robothor.engine.buddy import BuddyEngine

        # Must not raise — this hook is observational, never blocking.
        BuddyEngine().increment_task_count(agent_id="x")
