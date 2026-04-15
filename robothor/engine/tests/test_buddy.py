"""Tests for the Buddy gamification engine."""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

# ── XP and Level Math ───────────────────────────────────────────────────────


class TestXPLevelMath:
    """Tests for XP threshold and level computation."""

    def test_xp_for_level(self):
        from robothor.engine.buddy import xp_for_level

        assert xp_for_level(1) == 100
        assert xp_for_level(10) == 1000
        assert xp_for_level(50) == 5000

    def test_level_from_xp_zero(self):
        from robothor.engine.buddy import level_from_xp

        assert level_from_xp(0) == 1

    def test_level_from_xp_basic(self):
        from robothor.engine.buddy import level_from_xp

        # Level 1 requires 100 XP cumulative
        assert level_from_xp(100) == 1
        # Level 2 requires 100 + 200 = 300 cumulative
        assert level_from_xp(300) >= 2
        # Level 10 requires sum(100*i for i in 1..10) = 5500
        assert level_from_xp(5500) >= 10

    def test_level_from_xp_negative(self):
        from robothor.engine.buddy import level_from_xp

        assert level_from_xp(-100) == 1

    def test_level_monotonic(self):
        """More XP should never decrease the level."""
        from robothor.engine.buddy import level_from_xp

        prev_level = 1
        for xp in range(0, 10000, 100):
            level = level_from_xp(xp)
            assert level >= prev_level
            prev_level = level


# ── DailyStats ──────────────────────────────────────────────────────────────


class TestDailyStats:
    """Tests for the DailyStats dataclass."""

    def test_total_daily_xp_basic(self):
        from robothor.engine.buddy import DailyStats

        stats = DailyStats(
            stat_date=date(2026, 4, 3),
            tasks_completed=5,
            emails_processed=10,
            insights_generated=2,
            errors_avoided=1,
            dreams_completed=1,
        )
        # 5*10 + 10*5 + 2*20 + 1*15 + 1*10 = 50+50+40+15+10 = 165
        assert stats.total_daily_xp(streak_days=0) == 165

    def test_total_daily_xp_with_streak(self):
        from robothor.engine.buddy import DailyStats

        stats = DailyStats(
            stat_date=date(2026, 4, 3),
            tasks_completed=5,
            emails_processed=0,
            insights_generated=0,
            errors_avoided=0,
            dreams_completed=0,
        )
        # Base: 5*10 = 50. Streak bonus: 3*5 = 15. Total: 65
        assert stats.total_daily_xp(streak_days=3) == 65

    def test_total_daily_xp_zero_activity(self):
        from robothor.engine.buddy import DailyStats

        stats = DailyStats(stat_date=date(2026, 4, 3))
        assert stats.total_daily_xp() == 0

    def test_summary(self):
        from robothor.engine.buddy import DailyStats

        stats = DailyStats(
            stat_date=date(2026, 4, 3),
            tasks_completed=3,
            emails_processed=7,
            insights_generated=1,
            dreams_completed=2,
        )
        s = stats.summary()
        assert "Tasks:3" in s
        assert "Emails:7" in s


# ── LevelInfo ───────────────────────────────────────────────────────────────


class TestLevelInfo:
    """Tests for the LevelInfo dataclass."""

    def test_level_names(self):
        from robothor.engine.buddy import LevelInfo

        assert (
            LevelInfo(
                level=1, total_xp=0, xp_for_current_level=0, xp_for_next_level=100, progress_pct=0
            ).level_name
            == "Spark"
        )
        assert (
            LevelInfo(
                level=5, total_xp=0, xp_for_current_level=0, xp_for_next_level=0, progress_pct=0
            ).level_name
            == "Flame"
        )
        assert (
            LevelInfo(
                level=10, total_xp=0, xp_for_current_level=0, xp_for_next_level=0, progress_pct=0
            ).level_name
            == "Blaze"
        )
        assert (
            LevelInfo(
                level=20, total_xp=0, xp_for_current_level=0, xp_for_next_level=0, progress_pct=0
            ).level_name
            == "Inferno"
        )
        assert (
            LevelInfo(
                level=35, total_xp=0, xp_for_current_level=0, xp_for_next_level=0, progress_pct=0
            ).level_name
            == "Thunderstrike"
        )
        assert (
            LevelInfo(
                level=50, total_xp=0, xp_for_current_level=0, xp_for_next_level=0, progress_pct=0
            ).level_name
            == "Eternal Storm"
        )


# ── BuddyEngine ─────────────────────────────────────────────────────────────


def _mock_conn_ctx(cursor):
    """Create a mock connection context manager."""
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


class TestBuddyEngine:
    """Tests for the BuddyEngine class."""

    @patch("robothor.engine.buddy.BuddyEngine._compute_reliability", return_value=90)
    @patch("robothor.engine.buddy.BuddyEngine._compute_wisdom", return_value=60)
    @patch("robothor.engine.buddy.BuddyEngine._compute_chaos", return_value=20)
    @patch("robothor.engine.buddy.BuddyEngine._compute_patience", return_value=70)
    @patch("robothor.engine.buddy.BuddyEngine._compute_debugging", return_value=80)
    @patch("robothor.db.connection.get_connection")
    def test_compute_daily_stats(
        self, mock_conn, mock_dbg, mock_pat, mock_chaos, mock_wis, mock_rel
    ):
        cursor = MagicMock()
        # 4 queries: tasks_completed, emails, errors_avoided, dreams+insights
        call_idx = [0]
        results: list[list[tuple[int, ...]]] = [
            [(15,)],  # tasks_completed
            [(42,)],  # emails_processed
            [(3,)],  # errors_avoided
            [(2, 5)],  # dreams_completed=2, insights=5
        ]

        def fetchone():
            idx = call_idx[0]
            call_idx[0] += 1
            return results[idx][0] if idx < len(results) else None

        cursor.fetchone = fetchone
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        stats = engine.compute_daily_stats(date(2026, 4, 3))

        assert stats.tasks_completed == 15
        assert stats.emails_processed == 42
        assert stats.errors_avoided == 3
        assert stats.dreams_completed == 2
        assert stats.insights_generated == 5
        assert stats.debugging_score == 80
        assert stats.reliability_score == 90

    @patch("robothor.db.connection.get_connection")
    def test_get_level_info(self, mock_conn):
        cursor = MagicMock()
        cursor.fetchone.return_value = (2500, 7)
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        info = engine.get_level_info()

        assert info.total_xp == 2500
        assert info.level >= 1
        assert 0.0 <= info.progress_pct <= 1.0

    @patch("robothor.db.connection.get_connection")
    def test_get_level_info_empty_db(self, mock_conn):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        info = engine.get_level_info()

        assert info.level == 1
        assert info.total_xp == 0

    @patch("robothor.db.connection.get_connection")
    def test_get_streak(self, mock_conn):
        cursor = MagicMock()
        today = date(2026, 4, 3)
        # 3-day streak: today, yesterday, day before
        cursor.fetchall.return_value = [
            (today, 5),
            (today - timedelta(days=1), 3),
            (today - timedelta(days=2), 7),
            (today - timedelta(days=3), 0),  # break
            (today - timedelta(days=4), 2),
        ]
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        current, longest = engine.get_streak(today)

        assert current == 3
        assert longest == 3

    @patch("robothor.db.connection.get_connection")
    def test_get_streak_empty(self, mock_conn):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        current, longest = engine.get_streak()

        assert current == 0
        assert longest == 0

    @patch("robothor.db.connection.get_connection")
    def test_increment_task_count(self, mock_conn):
        cursor = MagicMock()
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        engine.increment_task_count()

        cursor.execute.assert_called_once()
        sql = cursor.execute.call_args[0][0]
        assert "buddy_stats" in sql
        assert "ON CONFLICT" in sql


# ── Buddy Hooks ─────────────────────────────────────────────────────────────


class TestBuddyHooks:
    """Tests for the buddy lifecycle hook handlers."""

    @patch("robothor.engine.buddy.BuddyEngine.increment_task_count")
    def test_on_agent_end_completed(self, mock_inc):
        from types import SimpleNamespace

        from robothor.engine.buddy_hooks import _on_agent_end

        # _on_agent_end expects a HookContext-like object with metadata dict
        ctx = SimpleNamespace(metadata={"status": "completed"}, agent_id="main", run_id="123")
        result = _on_agent_end(ctx)

        assert result["action"] == "allow"
        mock_inc.assert_called_once()

    @patch("robothor.engine.buddy.BuddyEngine.increment_task_count")
    def test_on_agent_end_failed_skips(self, mock_inc):
        from types import SimpleNamespace

        from robothor.engine.buddy_hooks import _on_agent_end

        ctx = SimpleNamespace(metadata={"status": "failed"}, agent_id="main", run_id="123")
        result = _on_agent_end(ctx)

        assert result["action"] == "allow"
        mock_inc.assert_not_called()

    def test_register_buddy_hooks(self):
        from robothor.engine.buddy_hooks import register_buddy_hooks

        registry = MagicMock()
        register_buddy_hooks(registry)

        registry.register.assert_called_once()
        hook = registry.register.call_args[0][0]
        assert hook.event.value == "agent_end"
        assert hook.handler_type == "python"
        assert hook.blocking is False


# ── refresh_daily ──────────────────────────────────────────────────────────


class TestRefreshDaily:
    """Tests for BuddyEngine.refresh_daily()."""

    @patch("robothor.engine.buddy.BuddyEngine._update_status_block")
    @patch("robothor.engine.buddy.BuddyEngine.get_streak", return_value=(3, 5))
    @patch("robothor.engine.buddy.BuddyEngine.get_level_info")
    @patch("robothor.db.connection.get_connection")
    @patch("robothor.engine.buddy.BuddyEngine.compute_daily_stats")
    def test_refresh_daily(self, mock_stats, mock_conn, mock_level, mock_streak, mock_block):
        from robothor.engine.buddy import BuddyEngine, DailyStats, LevelInfo

        mock_stats.return_value = DailyStats(
            stat_date=date(2026, 4, 3),
            tasks_completed=5,
            emails_processed=10,
            insights_generated=2,
            errors_avoided=1,
            dreams_completed=1,
        )
        mock_level.return_value = LevelInfo(
            level=5,
            total_xp=2000,
            xp_for_current_level=1500,
            xp_for_next_level=2100,
            progress_pct=0.83,
        )

        cursor = MagicMock()
        # fetchone() called after SUM query — return accumulated XP
        cursor.fetchone.return_value = (2180,)
        conn = MagicMock()
        conn.cursor.return_value = cursor
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = conn

        engine = BuddyEngine()
        result = engine.refresh_daily(date(2026, 4, 3))

        assert "daily_xp" in result
        assert "new_level" in result
        assert "leveled_up" in result
        assert "stats" in result
        assert result["stats"]["tasks"] == 5
        assert result["stats"]["streak"] == 3

    @patch("robothor.engine.buddy.BuddyEngine._update_status_block")
    @patch("robothor.engine.buddy.BuddyEngine.get_streak", return_value=(3, 5))
    @patch("robothor.engine.buddy.BuddyEngine.get_level_info")
    @patch("robothor.db.connection.get_connection")
    @patch("robothor.engine.buddy.BuddyEngine.compute_daily_stats")
    def test_refresh_daily_level_up(
        self, mock_stats, mock_conn, mock_level, mock_streak, mock_block
    ):
        from robothor.engine.buddy import BuddyEngine, DailyStats, LevelInfo

        mock_stats.return_value = DailyStats(
            stat_date=date(2026, 4, 3),
            tasks_completed=10,
            emails_processed=20,
            insights_generated=5,
            errors_avoided=3,
            dreams_completed=2,
        )
        # Level 6 requires cumulative 2100 XP. Set total_xp just below level 7 threshold.
        mock_level.return_value = LevelInfo(
            level=6,
            total_xp=2490,
            xp_for_current_level=2100,
            xp_for_next_level=2800,
            progress_pct=0.56,
        )

        cursor = MagicMock()
        # fetchone() is called once after the SUM query — return 2870 (2490 + 380)
        cursor.fetchone.return_value = (2870,)
        conn = MagicMock()
        conn.cursor.return_value = cursor
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = conn

        engine = BuddyEngine()
        result = engine.refresh_daily(date(2026, 4, 3))

        # daily_xp = 10*10 + 20*5 + 5*20 + 3*15 + 2*10 + 3*5 = 100+100+100+45+20+15 = 380
        # SUM(total_xp) from buddy_stats = 2870 → level_from_xp(2870) = 7
        assert result["leveled_up"] is True


# ── Buddy status context ──────────────────────────────────────────────────


class TestBuddyStatusContext:
    """Tests for _buddy_status_context in warmup.py."""

    @patch("robothor.engine.buddy.BuddyEngine.get_buddy_status")
    def test_buddy_status_context_main(self, mock_ctx):
        from robothor.engine.buddy import DailyStats, LevelInfo

        mock_ctx.return_value = {
            "level_info": LevelInfo(
                level=5,
                total_xp=2000,
                xp_for_current_level=1500,
                xp_for_next_level=2100,
                progress_pct=0.83,
            ),
            "streak": (3, 5),
            "scores_today": DailyStats(
                stat_date=date(2026, 4, 12),
                debugging_score=80,
                patience_score=70,
                chaos_score=20,
                wisdom_score=60,
                reliability_score=85,
            ),
            "score_deltas": {
                "debugging": 5,
                "patience": -2,
                "chaos": 0,
                "wisdom": 3,
                "reliability": 2,
            },
            "fleet_top": [{"agent_id": "email-classifier", "overall_score": 91, "rank": 1}],
            "overall_score": 78,
        }
        from robothor.engine.models import AgentConfig
        from robothor.engine.warmup import _buddy_status_context

        config = AgentConfig(id="main", name="Main")
        result = _buddy_status_context(config)
        assert result is not None
        assert "[FLEET PULSE]" in result
        assert "Level 5" in result
        assert "D:80" in result

    def test_buddy_status_context_non_main(self):
        from robothor.engine.models import AgentConfig
        from robothor.engine.warmup import _buddy_status_context

        config = AgentConfig(id="email-classifier", name="Email Classifier")
        result = _buddy_status_context(config)
        assert result is None

    @patch("robothor.engine.buddy.BuddyEngine.get_buddy_status")
    def test_buddy_status_context_no_events_in_warmup(self, mock_ctx):
        """Warmup context should never include events — those go through delivery."""
        from robothor.engine.buddy import DailyStats, LevelInfo

        mock_ctx.return_value = {
            "level_info": LevelInfo(
                level=10,
                total_xp=5500,
                xp_for_current_level=4500,
                xp_for_next_level=6000,
                progress_pct=0.67,
            ),
            "streak": (14, 14),
            "scores_today": DailyStats(stat_date=date(2026, 4, 12)),
            "score_deltas": {},
            "fleet_top": [],
            "overall_score": 75,
        }
        from robothor.engine.models import AgentConfig
        from robothor.engine.warmup import _buddy_status_context

        config = AgentConfig(id="main", name="Main")
        result = _buddy_status_context(config)
        assert result is not None
        # Should NOT contain event-like strings
        assert "milestone" not in (result or "").lower()

    @patch(
        "robothor.engine.buddy.BuddyEngine.get_buddy_status",
        side_effect=Exception("DB down"),
    )
    @patch("robothor.memory.blocks.read_block")
    def test_buddy_status_context_falls_back_to_block(self, mock_block, mock_ctx):
        """Falls back to stale memory block when live computation fails."""
        mock_block.return_value = {"content": "Level 5 Flame (2000 XP)"}
        from robothor.engine.models import AgentConfig
        from robothor.engine.warmup import _buddy_status_context

        config = AgentConfig(id="main", name="Main")
        result = _buddy_status_context(config)
        assert result is not None
        assert result.startswith("[BUDDY]")


# ── Standalone functions ───────────────────────────────────────────────────


class TestStandaloneFunctions:
    """Tests for module-level utility functions."""

    def test_level_name(self):
        from robothor.engine.buddy import level_name

        assert level_name(1) == "Spark"
        assert level_name(5) == "Flame"
        assert level_name(10) == "Blaze"
        assert level_name(20) == "Inferno"
        assert level_name(35) == "Thunderstrike"
        assert level_name(50) == "Eternal Storm"

    def test_compute_overall_score(self):
        from robothor.engine.buddy import compute_overall_score

        score = compute_overall_score(
            debugging=100, patience=100, effectiveness=100, benchmark_score=100, reliability=100
        )
        assert score == 100

    def test_compute_overall_score_all_zeros(self):
        from robothor.engine.buddy import compute_overall_score

        score = compute_overall_score(
            debugging=0, patience=0, effectiveness=0, benchmark_score=0, reliability=0
        )
        assert score == 0

    def test_compute_overall_score_mixed(self):
        from robothor.engine.buddy import compute_overall_score

        # 90*0.25 + 80*0.20 + 70*0.15 + 60*0.25 + 50*0.15 = 22.5+16+10.5+15+7.5 = 71.5 → 71
        score = compute_overall_score(
            debugging=80, patience=70, effectiveness=60, benchmark_score=50, reliability=90
        )
        assert score == 71


# ── AgentBuddyStats ───────────────────────────────────────────────────────


class TestAgentBuddyStats:
    """Tests for the per-agent stats dataclass."""

    def test_level_name_property(self):
        from robothor.engine.buddy import AgentBuddyStats

        stats = AgentBuddyStats(agent_id="test", stat_date=date(2026, 4, 4), level=10)
        assert stats.level_name == "Blaze"

    def test_defaults(self):
        from robothor.engine.buddy import AgentBuddyStats

        stats = AgentBuddyStats(agent_id="test", stat_date=date(2026, 4, 4))
        assert stats.overall_score == 50
        assert stats.rank == 0
        assert stats.last_benchmark_score is None


# ── Per-agent compute ──────────────────────────────────────────────────────


class TestPerAgentCompute:
    """Tests for per-agent score computation."""

    @patch("robothor.engine.buddy.BuddyEngine._get_latest_benchmark", return_value=(None, None))
    @patch("robothor.engine.buddy.BuddyEngine._get_agent_total_xp", return_value=500)
    @patch("robothor.engine.buddy.BuddyEngine._compute_reliability", return_value=90)
    @patch("robothor.engine.buddy.BuddyEngine._compute_wisdom", return_value=60)
    @patch("robothor.engine.buddy.BuddyEngine._compute_chaos", return_value=20)
    @patch("robothor.engine.buddy.BuddyEngine._compute_patience", return_value=70)
    @patch("robothor.engine.buddy.BuddyEngine._compute_debugging", return_value=80)
    @patch("robothor.engine.buddy.BuddyEngine._agent_run_count", return_value=10)
    @patch("robothor.db.connection.get_connection")
    def test_compute_agent_scores(
        self,
        mock_conn,
        mock_runcount,
        mock_dbg,
        mock_pat,
        mock_chaos,
        mock_wis,
        mock_rel,
        mock_xp,
        mock_bench,
    ):
        cursor = MagicMock()
        call_idx = [0]
        results = [
            [(5,)],  # tasks_completed
            [(10,)],  # emails_processed
            [(1,)],  # errors_avoided
        ]

        def fetchone():
            idx = call_idx[0]
            call_idx[0] += 1
            return results[idx][0] if idx < len(results) else None

        cursor.fetchone = fetchone
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        agent_stats = engine.compute_agent_scores("email-classifier", date(2026, 4, 4))

        assert agent_stats is not None
        assert agent_stats.agent_id == "email-classifier"
        assert agent_stats.tasks_completed == 5
        assert agent_stats.debugging_score == 80
        assert agent_stats.reliability_score == 90
        assert agent_stats.overall_score > 0
        # XP: 5*10 + 1*15 = 65, total = 500 + 65 = 565
        assert agent_stats.daily_xp == 65
        assert agent_stats.total_xp == 565
        assert agent_stats.level >= 1

    @patch("robothor.engine.buddy.BuddyEngine._agent_run_count", return_value=1)
    def test_compute_agent_scores_insufficient_runs(self, mock_runcount):
        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        result = engine.compute_agent_scores("brand-new-agent", date(2026, 4, 4))

        assert result is None

    @patch("robothor.engine.buddy.BuddyEngine.compute_agent_scores")
    @patch("robothor.engine.buddy.BuddyEngine._get_active_agent_ids")
    def test_compute_fleet_scores(self, mock_ids, mock_scores):
        from robothor.engine.buddy import AgentBuddyStats, BuddyEngine

        target = date(2026, 4, 4)
        mock_ids.return_value = ["agent-a", "agent-b", "agent-c"]

        def make_stats(agent_id):
            overall = {"agent-a": 90, "agent-b": 50, "agent-c": 75}[agent_id]
            return AgentBuddyStats(agent_id=agent_id, stat_date=target, overall_score=overall)

        mock_scores.side_effect = lambda aid, td: make_stats(aid)

        engine = BuddyEngine()
        fleet = engine.compute_fleet_scores(target)

        assert len(fleet) == 3
        # Sorted by overall desc
        assert fleet[0].agent_id == "agent-a"
        assert fleet[0].rank == 1
        assert fleet[1].agent_id == "agent-c"
        assert fleet[1].rank == 2
        assert fleet[2].agent_id == "agent-b"
        assert fleet[2].rank == 3

    @patch("robothor.db.connection.get_connection")
    def test_increment_task_count_per_agent(self, mock_conn):
        cursor = MagicMock()
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        engine.increment_task_count(agent_id="email-classifier")

        # Should have 2 execute calls: global + per-agent
        assert cursor.execute.call_count == 2
        calls = [c[0][0] for c in cursor.execute.call_args_list]
        assert "buddy_stats" in calls[0]
        assert "agent_buddy_stats" in calls[1]

    @patch("robothor.db.connection.get_connection")
    def test_increment_task_count_no_agent_id(self, mock_conn):
        cursor = MagicMock()
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        engine.increment_task_count(agent_id=None)

        # Should have 1 execute call: global only
        assert cursor.execute.call_count == 1
        assert "buddy_stats" in cursor.execute.call_args[0][0]


# ── Agent filter helper ───────────────────────────────────────────────────


class TestAgentClause:
    """Tests for the _agent_clause SQL helper."""

    def test_no_agent_id(self):
        from robothor.engine.buddy import _agent_clause

        clause, params = _agent_clause(None)
        assert clause == ""
        assert params == []

    def test_with_agent_id(self):
        from robothor.engine.buddy import _agent_clause

        clause, params = _agent_clause("email-classifier")
        assert "agent_id" in clause
        assert params == ["email-classifier"]
        assert "f'" not in clause  # no f-strings in output

    def test_with_column_qualifier(self):
        from robothor.engine.buddy import _agent_clause

        clause, params = _agent_clause("email-classifier", column="r.agent_id")
        assert "r.agent_id" in clause
        assert params == ["email-classifier"]

    def test_no_agent_id_with_column(self):
        from robothor.engine.buddy import _agent_clause

        clause, params = _agent_clause(None, column="r.agent_id")
        assert clause == ""
        assert params == []


# ── Per-agent wisdom neutral default ──────────────────────────────────────


class TestPerAgentWisdomDefault:
    """Verify that per-agent scoring uses neutral wisdom (50) since dreams are global."""

    @patch("robothor.engine.buddy.BuddyEngine._get_latest_benchmark", return_value=(None, None))
    @patch("robothor.engine.buddy.BuddyEngine._get_agent_total_xp", return_value=0)
    @patch("robothor.engine.buddy.BuddyEngine._compute_reliability", return_value=80)
    @patch("robothor.engine.buddy.BuddyEngine._compute_chaos", return_value=20)
    @patch("robothor.engine.buddy.BuddyEngine._compute_patience", return_value=70)
    @patch("robothor.engine.buddy.BuddyEngine._compute_debugging", return_value=80)
    @patch("robothor.engine.buddy.BuddyEngine._agent_run_count", return_value=10)
    @patch("robothor.db.connection.get_connection")
    def test_wisdom_defaults_to_50(
        self,
        mock_conn,
        mock_runcount,
        mock_dbg,
        mock_pat,
        mock_chaos,
        mock_rel,
        mock_xp,
        mock_bench,
    ):
        cursor = MagicMock()
        results = [[(5,)], [(0,)], [(1,)]]
        call_idx = [0]

        def fetchone():
            idx = call_idx[0]
            call_idx[0] += 1
            return results[idx][0] if idx < len(results) else None

        cursor.fetchone = fetchone
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        agent_stats = engine.compute_agent_scores("test-agent", date(2026, 4, 4))

        assert agent_stats is not None
        assert agent_stats.wisdom_score == 50


# ── Buddy hooks with agent_id ─────────────────────────────────────────────


class TestBuddyHooksPerAgent:
    """Tests for the buddy hook passing agent_id."""

    @patch("robothor.engine.buddy.BuddyEngine.increment_task_count")
    def test_on_agent_end_passes_agent_id(self, mock_inc):
        from types import SimpleNamespace

        from robothor.engine.buddy_hooks import _on_agent_end

        ctx = SimpleNamespace(
            metadata={"status": "completed", "agent_id": "email-classifier"},
            agent_id="email-classifier",
            run_id="123",
        )
        result = _on_agent_end(ctx)

        assert result["action"] == "allow"
        mock_inc.assert_called_once_with(agent_id="email-classifier")

    @patch("robothor.engine.buddy.BuddyEngine.increment_task_count")
    def test_on_agent_end_falls_back_to_context_attr(self, mock_inc):
        from types import SimpleNamespace

        from robothor.engine.buddy_hooks import _on_agent_end

        ctx = SimpleNamespace(
            metadata={"status": "completed"},
            agent_id="morning-briefing",
            run_id="456",
        )
        result = _on_agent_end(ctx)

        assert result["action"] == "allow"
        mock_inc.assert_called_once_with(agent_id="morning-briefing")


# ── Underperformer flagging ───────────────────────────────────────────────


class TestFlagUnderperformers:
    """Tests for the AutoAgent integration."""

    @patch("robothor.engine.buddy.BuddyEngine._create_autoagent_task", return_value=True)
    @patch("robothor.db.connection.get_connection")
    def test_flag_underperformers_finds_agents(self, mock_conn, mock_task):
        cursor = MagicMock()
        cursor.fetchall.return_value = [("slow-agent",), ("broken-agent",)]
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        flagged = engine.flag_underperformers(threshold=40, consecutive_days=3)

        assert flagged == ["slow-agent", "broken-agent"]
        assert mock_task.call_count == 2

    @patch("robothor.db.connection.get_connection")
    def test_flag_underperformers_none_found(self, mock_conn):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        flagged = engine.flag_underperformers()

        assert flagged == []


# ── Benchmark integration ─────────────────────────────────────────────────


class TestBenchmarkIntegration:
    """Tests for reading benchmark scores from memory blocks."""

    @patch("robothor.memory.blocks.read_block")
    def test_get_latest_benchmark(self, mock_read):
        mock_read.return_value = {
            "content": json.dumps(
                {
                    "aggregate_score": 0.85,
                    "timestamp": "2026-04-04T12:00:00+00:00",
                }
            )
        }

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        score, ts = engine._get_latest_benchmark("email-classifier")

        assert score == 0.85
        assert ts is not None
        mock_read.assert_called_once_with("agent_benchmark_latest:email-classifier")

    @patch("robothor.memory.blocks.read_block")
    def test_get_latest_benchmark_missing(self, mock_read):
        mock_read.return_value = None

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        score, ts = engine._get_latest_benchmark("no-benchmarks")

        assert score is None
        assert ts is None


# ── Heartbeat Context ────────────────────────────────────────────────────


class TestGetBuddyHeartbeatContext:
    """Tests for BuddyEngine.get_buddy_heartbeat_context() — TDD.

    These use the legacy wrapper which delegates to get_buddy_events().
    Cooldowns are mocked to empty state so events pass through.
    """

    @patch("robothor.engine.buddy.BuddyEngine._save_event_cooldowns")
    @patch("robothor.engine.buddy.BuddyEngine._load_event_cooldowns", return_value={})
    @patch("robothor.engine.buddy.BuddyEngine.compute_fleet_scores")
    @patch("robothor.engine.buddy.BuddyEngine.compute_daily_stats")
    @patch("robothor.engine.buddy.BuddyEngine.get_streak")
    @patch("robothor.engine.buddy.BuddyEngine.get_level_info")
    @patch("robothor.db.connection.get_connection")
    def test_returns_all_expected_keys(
        self, mock_conn, mock_level, mock_streak, mock_stats, mock_fleet, _cd_load, _cd_save
    ):
        from robothor.engine.buddy import (
            AgentBuddyStats,
            BuddyEngine,
            DailyStats,
            LevelInfo,
        )

        today = date(2026, 4, 12)
        mock_level.return_value = LevelInfo(
            level=12,
            total_xp=8400,
            xp_for_current_level=7800,
            xp_for_next_level=9100,
            progress_pct=0.46,
        )
        mock_streak.return_value = (14, 14)
        mock_stats.return_value = DailyStats(
            stat_date=today,
            debugging_score=87,
            patience_score=72,
            effectiveness_score=80,
            benchmark_dim_score=65,
            reliability_score=92,
        )
        mock_fleet.return_value = [
            AgentBuddyStats(agent_id="email-classifier", stat_date=today, overall_score=91, rank=1),
            AgentBuddyStats(agent_id="calendar-monitor", stat_date=today, overall_score=88, rank=2),
        ]

        # Mock yesterday's scores: debugging, patience, effectiveness, benchmark, reliability
        cursor = MagicMock()
        cursor.fetchone.return_value = (82, 75, 75, 60, 87)
        mock_conn.return_value = _mock_conn_ctx(cursor)

        engine = BuddyEngine()
        ctx = engine.get_buddy_heartbeat_context(today)

        assert "level_info" in ctx
        assert "streak" in ctx
        assert "scores_today" in ctx
        assert "score_deltas" in ctx
        assert "fleet_top" in ctx
        assert "events" in ctx
        assert ctx["level_info"].level == 12
        assert ctx["streak"] == (14, 14)

    @patch("robothor.engine.buddy.BuddyEngine._save_event_cooldowns")
    @patch("robothor.engine.buddy.BuddyEngine._load_event_cooldowns", return_value={})
    @patch("robothor.engine.buddy.BuddyEngine.compute_fleet_scores")
    @patch("robothor.engine.buddy.BuddyEngine.compute_daily_stats")
    @patch("robothor.engine.buddy.BuddyEngine.get_streak")
    @patch("robothor.engine.buddy.BuddyEngine.get_level_info")
    @patch("robothor.db.connection.get_connection")
    def test_detects_streak_milestone(
        self, mock_conn, mock_level, mock_streak, mock_stats, mock_fleet, _cd_load, _cd_save
    ):
        from robothor.engine.buddy import BuddyEngine, DailyStats, LevelInfo

        today = date(2026, 4, 12)
        mock_level.return_value = LevelInfo(
            level=5,
            total_xp=2000,
            xp_for_current_level=1500,
            xp_for_next_level=2100,
            progress_pct=0.83,
        )
        mock_streak.return_value = (7, 7)  # milestone!
        mock_stats.return_value = DailyStats(stat_date=today)
        mock_fleet.return_value = []

        cursor = MagicMock()
        cursor.fetchone.return_value = None  # no yesterday data
        mock_conn.return_value = _mock_conn_ctx(cursor)

        engine = BuddyEngine()
        ctx = engine.get_buddy_heartbeat_context(today)

        events = ctx["events"]
        assert any("streak" in e.lower() for e in events)

    @patch("robothor.engine.buddy.BuddyEngine._save_event_cooldowns")
    @patch("robothor.engine.buddy.BuddyEngine._load_event_cooldowns", return_value={})
    @patch("robothor.engine.buddy.BuddyEngine.compute_fleet_scores")
    @patch("robothor.engine.buddy.BuddyEngine.compute_daily_stats")
    @patch("robothor.engine.buddy.BuddyEngine.get_streak")
    @patch("robothor.engine.buddy.BuddyEngine.get_level_info")
    @patch("robothor.db.connection.get_connection")
    def test_detects_level_up(
        self, mock_conn, mock_level, mock_streak, mock_stats, mock_fleet, _cd_load, _cd_save
    ):
        from robothor.engine.buddy import BuddyEngine, DailyStats, LevelInfo

        today = date(2026, 4, 12)
        mock_level.return_value = LevelInfo(
            level=10,
            total_xp=5500,
            xp_for_current_level=4500,
            xp_for_next_level=6000,
            progress_pct=0.67,
        )
        mock_streak.return_value = (3, 10)
        mock_stats.return_value = DailyStats(stat_date=today)
        mock_fleet.return_value = []

        cursor = MagicMock()
        # Yesterday's level was 9 (via buddy_stats row)
        cursor.fetchone.side_effect = [
            (50, 50, 50, 50, 50),  # yesterday's scores
            (9,),  # yesterday's level
        ]
        mock_conn.return_value = _mock_conn_ctx(cursor)

        engine = BuddyEngine()
        ctx = engine.get_buddy_heartbeat_context(today)

        events = ctx["events"]
        assert any("level" in e.lower() for e in events)

    @patch("robothor.engine.buddy.BuddyEngine._save_event_cooldowns")
    @patch("robothor.engine.buddy.BuddyEngine._load_event_cooldowns", return_value={})
    @patch("robothor.engine.buddy.BuddyEngine.compute_fleet_scores")
    @patch("robothor.engine.buddy.BuddyEngine.compute_daily_stats")
    @patch("robothor.engine.buddy.BuddyEngine.get_streak")
    @patch("robothor.engine.buddy.BuddyEngine.get_level_info")
    @patch("robothor.db.connection.get_connection")
    def test_computes_score_deltas(
        self, mock_conn, mock_level, mock_streak, mock_stats, mock_fleet, _cd_load, _cd_save
    ):
        from robothor.engine.buddy import BuddyEngine, DailyStats, LevelInfo

        today = date(2026, 4, 12)
        mock_level.return_value = LevelInfo(
            level=5,
            total_xp=2000,
            xp_for_current_level=1500,
            xp_for_next_level=2100,
            progress_pct=0.83,
        )
        mock_streak.return_value = (3, 5)
        mock_stats.return_value = DailyStats(
            stat_date=today,
            debugging_score=87,
            patience_score=72,
            effectiveness_score=80,
            benchmark_dim_score=65,
            reliability_score=92,
        )
        mock_fleet.return_value = []

        cursor = MagicMock()
        # Yesterday's scores: debugging=80, patience=75, effectiveness=75, benchmark=60, reliability=87
        cursor.fetchone.return_value = (80, 75, 75, 60, 87)
        mock_conn.return_value = _mock_conn_ctx(cursor)

        engine = BuddyEngine()
        ctx = engine.get_buddy_heartbeat_context(today)

        deltas = ctx["score_deltas"]
        assert deltas["debugging"] == 7  # 87 - 80
        assert deltas["patience"] == -3  # 72 - 75
        assert deltas["effectiveness"] == 5  # 80 - 75
        assert deltas["benchmark"] == 5  # 65 - 60
        assert deltas["reliability"] == 5  # 92 - 87

    @patch("robothor.engine.buddy.BuddyEngine._save_event_cooldowns")
    @patch("robothor.engine.buddy.BuddyEngine._load_event_cooldowns", return_value={})
    @patch("robothor.engine.buddy.BuddyEngine.compute_fleet_scores")
    @patch("robothor.engine.buddy.BuddyEngine.compute_daily_stats")
    @patch("robothor.engine.buddy.BuddyEngine.get_streak")
    @patch("robothor.engine.buddy.BuddyEngine.get_level_info")
    @patch("robothor.db.connection.get_connection")
    def test_no_events_when_stable(
        self, mock_conn, mock_level, mock_streak, mock_stats, mock_fleet, _cd_load, _cd_save
    ):
        from robothor.engine.buddy import BuddyEngine, DailyStats, LevelInfo

        today = date(2026, 4, 12)
        mock_level.return_value = LevelInfo(
            level=5,
            total_xp=2000,
            xp_for_current_level=1500,
            xp_for_next_level=2100,
            progress_pct=0.83,
        )
        mock_streak.return_value = (3, 5)  # no milestone
        mock_stats.return_value = DailyStats(
            stat_date=today,
            debugging_score=80,
            patience_score=70,
            effectiveness_score=60,
            benchmark_dim_score=55,
            reliability_score=85,
        )
        mock_fleet.return_value = []

        cursor = MagicMock()
        # Yesterday's scores are almost the same (all deltas < 5)
        cursor.fetchone.return_value = (78, 72, 58, 53, 83)
        mock_conn.return_value = _mock_conn_ctx(cursor)

        engine = BuddyEngine()
        ctx = engine.get_buddy_heartbeat_context(today)

        assert ctx["events"] == []


# ── Escalation to auto-researcher ────────────────────────────────────────


class TestEscalationPath:
    """Tests for escalating repeat underperformers to auto-researcher."""

    @patch("robothor.engine.buddy.BuddyEngine._create_autoagent_task", return_value=True)
    @patch("robothor.engine.buddy.BuddyEngine._get_flag_count", return_value=1)
    @patch("robothor.db.connection.get_connection")
    def test_first_flag_goes_to_autoagent(self, mock_conn, mock_count, mock_task):
        cursor = MagicMock()
        cursor.fetchall.return_value = [("slow-agent",)]
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        flagged = engine.flag_underperformers(threshold=70, consecutive_days=2)

        assert flagged == ["slow-agent"]
        mock_task.assert_called_once_with("slow-agent", 70)

    @patch("robothor.engine.buddy.BuddyEngine._create_researcher_task", return_value=True)
    @patch("robothor.engine.buddy.BuddyEngine._create_autoagent_task")
    @patch("robothor.engine.buddy.BuddyEngine._get_flag_count", return_value=3)
    @patch("robothor.db.connection.get_connection")
    def test_third_flag_escalates_to_researcher(
        self, mock_conn, mock_count, mock_agent_task, mock_researcher_task
    ):
        cursor = MagicMock()
        cursor.fetchall.return_value = [("stubborn-agent",)]
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        flagged = engine.flag_underperformers(threshold=70, consecutive_days=2)

        assert flagged == ["stubborn-agent"]
        mock_agent_task.assert_not_called()
        mock_researcher_task.assert_called_once_with("stubborn-agent", 70)


# ── Event cooldown system ──────────────────────────────────────────────────


class TestEventCooldowns:
    """Tests for buddy event cooldown infrastructure."""

    def test_empty_cooldown_state_allows_all(self):
        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        state: dict[str, str] = {}
        assert not engine._is_on_cooldown(state, "level_up")
        assert not engine._is_on_cooldown(state, "streak_milestone")

    def test_mark_and_check_cooldown(self):
        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        state: dict[str, str] = {}
        engine._mark_event_fired(state, "level_up")
        assert engine._is_on_cooldown(state, "level_up")

    def test_expired_cooldown_allows_event(self):
        from datetime import UTC, datetime, timedelta

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        # Simulate a level_up fired 25 hours ago (cooldown is 24h)
        old_time = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        state = {"cooldown_level_up": old_time}
        assert not engine._is_on_cooldown(state, "level_up")

    def test_active_cooldown_blocks_event(self):
        from datetime import UTC, datetime, timedelta

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        # Simulate a level_up fired 1 hour ago (cooldown is 24h)
        recent_time = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        state = {"cooldown_level_up": recent_time}
        assert engine._is_on_cooldown(state, "level_up")

    def test_corrupt_cooldown_state_allows_event(self):
        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        state = {"cooldown_level_up": "not-a-timestamp"}
        assert not engine._is_on_cooldown(state, "level_up")


class TestGetBuddyStatus:
    """Tests for get_buddy_status() — status-only, no events."""

    @patch("robothor.engine.buddy.BuddyEngine.compute_fleet_scores")
    @patch("robothor.engine.buddy.BuddyEngine.compute_daily_stats")
    @patch("robothor.engine.buddy.BuddyEngine.get_streak")
    @patch("robothor.engine.buddy.BuddyEngine.get_level_info")
    @patch("robothor.db.connection.get_connection")
    def test_returns_status_without_events(
        self, mock_conn, mock_level, mock_streak, mock_stats, mock_fleet
    ):
        from robothor.engine.buddy import BuddyEngine, DailyStats, LevelInfo

        today = date(2026, 4, 12)
        mock_level.return_value = LevelInfo(
            level=10,
            total_xp=5500,
            xp_for_current_level=4500,
            xp_for_next_level=6000,
            progress_pct=0.67,
        )
        mock_streak.return_value = (14, 14)
        mock_stats.return_value = DailyStats(stat_date=today, debugging_score=87)
        mock_fleet.return_value = []

        cursor = MagicMock()
        cursor.fetchone.return_value = None
        mock_conn.return_value = _mock_conn_ctx(cursor)

        ctx = BuddyEngine().get_buddy_status(today)

        assert "level_info" in ctx
        assert "streak" in ctx
        assert "scores_today" in ctx
        assert "events" not in ctx
        assert "yesterday_level" not in ctx


class TestGetBuddyEventsDedup:
    """Tests for get_buddy_events() — events fire once, then cooldown blocks them."""

    @patch("robothor.engine.buddy.BuddyEngine._save_event_cooldowns")
    @patch("robothor.engine.buddy.BuddyEngine._load_event_cooldowns", return_value={})
    @patch("robothor.engine.buddy.BuddyEngine.compute_fleet_scores")
    @patch("robothor.engine.buddy.BuddyEngine.compute_daily_stats")
    @patch("robothor.engine.buddy.BuddyEngine.get_streak")
    @patch("robothor.engine.buddy.BuddyEngine.get_level_info")
    @patch("robothor.db.connection.get_connection")
    def test_level_up_fires_once(
        self, mock_conn, mock_level, mock_streak, mock_stats, mock_fleet, mock_cd_load, mock_cd_save
    ):
        from robothor.engine.buddy import BuddyEngine, DailyStats, LevelInfo

        today = date(2026, 4, 12)
        mock_level.return_value = LevelInfo(
            level=4,
            total_xp=1000,
            xp_for_current_level=600,
            xp_for_next_level=1500,
            progress_pct=0.4,
        )
        mock_streak.return_value = (3, 5)
        mock_stats.return_value = DailyStats(stat_date=today)
        mock_fleet.return_value = []

        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (50, 50, 50, 50, 50),  # yesterday's scores
            (1,),  # yesterday's level (level up from 1 to 4)
        ]
        mock_conn.return_value = _mock_conn_ctx(cursor)

        engine = BuddyEngine()

        # First call — event fires
        ctx = engine.get_buddy_events(today)
        assert any("level" in e.lower() for e in ctx["events"])
        mock_cd_save.assert_called_once()

        # Capture the cooldown state that was saved
        saved_state = mock_cd_save.call_args[0][0]
        assert "cooldown_level_up" in saved_state

    @patch("robothor.engine.buddy.BuddyEngine._save_event_cooldowns")
    @patch("robothor.engine.buddy.BuddyEngine._load_event_cooldowns")
    @patch("robothor.engine.buddy.BuddyEngine.compute_fleet_scores")
    @patch("robothor.engine.buddy.BuddyEngine.compute_daily_stats")
    @patch("robothor.engine.buddy.BuddyEngine.get_streak")
    @patch("robothor.engine.buddy.BuddyEngine.get_level_info")
    @patch("robothor.db.connection.get_connection")
    def test_level_up_blocked_on_second_call(
        self, mock_conn, mock_level, mock_streak, mock_stats, mock_fleet, mock_cd_load, mock_cd_save
    ):
        from datetime import UTC, datetime

        from robothor.engine.buddy import BuddyEngine, DailyStats, LevelInfo

        today = date(2026, 4, 12)
        mock_level.return_value = LevelInfo(
            level=4,
            total_xp=1000,
            xp_for_current_level=600,
            xp_for_next_level=1500,
            progress_pct=0.4,
        )
        mock_streak.return_value = (3, 5)
        mock_stats.return_value = DailyStats(stat_date=today)
        mock_fleet.return_value = []

        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (50, 50, 50, 50, 50),
            (1,),
        ]
        mock_conn.return_value = _mock_conn_ctx(cursor)

        # Simulate cooldown already set from a previous call
        mock_cd_load.return_value = {"cooldown_level_up": datetime.now(UTC).isoformat()}

        engine = BuddyEngine()
        ctx = engine.get_buddy_events(today)

        # Level-up should be blocked by cooldown
        assert not any("level" in e.lower() for e in ctx["events"])
        mock_cd_save.assert_not_called()

    @patch("robothor.engine.buddy.BuddyEngine._save_event_cooldowns")
    @patch("robothor.engine.buddy.BuddyEngine._load_event_cooldowns", return_value={})
    @patch("robothor.engine.buddy.BuddyEngine.compute_fleet_scores")
    @patch("robothor.engine.buddy.BuddyEngine.compute_daily_stats")
    @patch("robothor.engine.buddy.BuddyEngine.get_streak")
    @patch("robothor.engine.buddy.BuddyEngine.get_level_info")
    @patch("robothor.db.connection.get_connection")
    def test_no_events_means_no_cooldown_save(
        self, mock_conn, mock_level, mock_streak, mock_stats, mock_fleet, mock_cd_load, mock_cd_save
    ):
        from robothor.engine.buddy import BuddyEngine, DailyStats, LevelInfo

        today = date(2026, 4, 12)
        mock_level.return_value = LevelInfo(
            level=5,
            total_xp=2000,
            xp_for_current_level=1500,
            xp_for_next_level=2100,
            progress_pct=0.83,
        )
        mock_streak.return_value = (3, 5)  # no milestone
        mock_stats.return_value = DailyStats(
            stat_date=today,
            debugging_score=80,
            patience_score=70,
            effectiveness_score=60,
            benchmark_dim_score=55,
            reliability_score=85,
        )
        mock_fleet.return_value = []

        cursor = MagicMock()
        cursor.fetchone.return_value = (78, 72, 58, 53, 83)
        mock_conn.return_value = _mock_conn_ctx(cursor)

        engine = BuddyEngine()
        ctx = engine.get_buddy_events(today)

        assert ctx["events"] == []
        mock_cd_save.assert_not_called()


# ── Effectiveness dimension (replaces Chaos) ────────────────────────────


class TestComputeEffectiveness:
    """Tests for _compute_effectiveness — outcome-based scoring.

    Replaces the old Chaos dimension. Reads outcome_assessment from
    agent_runs and computes satisfaction_rate * 100.
    """

    @patch("robothor.db.connection.get_connection")
    def test_all_successful(self, mock_conn):
        """100% successful outcomes → effectiveness = 100."""
        cursor = MagicMock()
        cursor.fetchall.return_value = [("successful", 10)]
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        score = engine._compute_effectiveness(date(2026, 4, 14), agent_id="test-agent")
        assert score == 100

    @patch("robothor.db.connection.get_connection")
    def test_mixed_outcomes(self, mock_conn):
        """7 successful + 3 partial/incorrect → effectiveness = 70."""
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            ("successful", 7),
            ("partial", 2),
            ("incorrect", 1),
        ]
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        score = engine._compute_effectiveness(date(2026, 4, 14), agent_id="test-agent")
        assert score == 70

    @patch("robothor.db.connection.get_connection")
    def test_no_assessed_runs(self, mock_conn):
        """No outcome_assessment data → neutral 50."""
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        score = engine._compute_effectiveness(date(2026, 4, 14), agent_id="test-agent")
        assert score == 50

    @patch("robothor.db.connection.get_connection")
    def test_all_incorrect(self, mock_conn):
        """All outcomes are incorrect → effectiveness = 0."""
        cursor = MagicMock()
        cursor.fetchall.return_value = [("incorrect", 5)]
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        score = engine._compute_effectiveness(date(2026, 4, 14), agent_id="test-agent")
        assert score == 0

    @patch("robothor.db.connection.get_connection")
    def test_abandoned_excluded(self, mock_conn):
        """Abandoned runs are excluded from scoring (like analytics.py)."""
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            ("successful", 8),
            ("partial", 2),
            ("abandoned", 5),  # should be excluded
        ]
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        score = engine._compute_effectiveness(date(2026, 4, 14), agent_id="test-agent")
        assert score == 80  # 8 / (8+2) = 0.80

    @patch("robothor.db.connection.get_connection")
    def test_global_mode_no_agent_id(self, mock_conn):
        """Works without agent_id for global scoring."""
        cursor = MagicMock()
        cursor.fetchall.return_value = [("successful", 10), ("partial", 5)]
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        score = engine._compute_effectiveness(date(2026, 4, 14))
        assert score == 66  # int(100 * 10/15) = 66

    @patch("robothor.db.connection.get_connection", side_effect=Exception("DB down"))
    def test_exception_returns_neutral(self, mock_conn):
        """DB errors return neutral 50."""
        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        score = engine._compute_effectiveness(date(2026, 4, 14))
        assert score == 50


# ── Benchmark dimension (replaces Wisdom) ────────────────────────────


class TestComputeBenchmarkScore:
    """Tests for _compute_benchmark_score — reads latest benchmark aggregate.

    Replaces the old Wisdom dimension (which was hardcoded to 50 per-agent).
    """

    @patch("robothor.memory.blocks.read_block")
    def test_with_benchmark(self, mock_read):
        """Benchmark score 0.85 → dimension score 85."""
        mock_read.return_value = {
            "content": json.dumps(
                {
                    "aggregate_score": 0.85,
                    "timestamp": "2026-04-14T12:00:00+00:00",
                }
            )
        }

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        score = engine._compute_benchmark_score("email-classifier")
        assert score == 85

    @patch("robothor.memory.blocks.read_block")
    def test_perfect_benchmark(self, mock_read):
        """Benchmark score 1.0 → dimension score 100."""
        mock_read.return_value = {
            "content": json.dumps(
                {
                    "aggregate_score": 1.0,
                    "timestamp": "2026-04-14T12:00:00+00:00",
                }
            )
        }

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        score = engine._compute_benchmark_score("test-agent")
        assert score == 100

    @patch("robothor.memory.blocks.read_block")
    def test_no_benchmark(self, mock_read):
        """No benchmark data → neutral 50."""
        mock_read.return_value = None

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        score = engine._compute_benchmark_score("new-agent")
        assert score == 50

    @patch("robothor.memory.blocks.read_block")
    def test_zero_benchmark(self, mock_read):
        """Benchmark score 0.0 → dimension score 0."""
        mock_read.return_value = {
            "content": json.dumps(
                {
                    "aggregate_score": 0.0,
                    "timestamp": "2026-04-14T12:00:00+00:00",
                }
            )
        }

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        score = engine._compute_benchmark_score("failing-agent")
        assert score == 0


# ── Updated overall score formula ────────────────────────────────────


class TestUpdatedOverallScore:
    """Tests for updated compute_overall_score with effectiveness + benchmark."""

    def test_new_signature(self):
        """compute_overall_score accepts effectiveness and benchmark_score params."""
        from robothor.engine.buddy import compute_overall_score

        score = compute_overall_score(
            debugging=80,
            patience=70,
            effectiveness=90,
            benchmark_score=85,
            reliability=95,
        )
        # 95*0.25 + 80*0.20 + 70*0.15 + 90*0.25 + 85*0.15
        # = 23.75 + 16.0 + 10.5 + 22.5 + 12.75 = 85.5 → 85
        assert score == 85

    def test_all_zeros(self):
        from robothor.engine.buddy import compute_overall_score

        score = compute_overall_score(
            debugging=0, patience=0, effectiveness=0, benchmark_score=0, reliability=0
        )
        assert score == 0

    def test_all_hundreds(self):
        from robothor.engine.buddy import compute_overall_score

        score = compute_overall_score(
            debugging=100, patience=100, effectiveness=100, benchmark_score=100, reliability=100
        )
        assert score == 100

    def test_weights_sum_to_one(self):
        """Verify that weight constants sum to 1.0."""
        from robothor.engine.buddy import (
            WEIGHT_BENCHMARK,
            WEIGHT_DEBUGGING,
            WEIGHT_EFFECTIVENESS,
            WEIGHT_PATIENCE,
            WEIGHT_RELIABILITY,
        )

        total = (
            WEIGHT_RELIABILITY
            + WEIGHT_DEBUGGING
            + WEIGHT_PATIENCE
            + WEIGHT_EFFECTIVENESS
            + WEIGHT_BENCHMARK
        )
        assert abs(total - 1.0) < 0.001


# ── Updated dataclasses ──────────────────────────────────────────────


class TestUpdatedDataclasses:
    """Tests for updated DailyStats and AgentBuddyStats."""

    def test_daily_stats_has_new_fields(self):
        from robothor.engine.buddy import DailyStats

        stats = DailyStats(
            stat_date=date(2026, 4, 14),
            effectiveness_score=80,
            benchmark_dim_score=75,
        )
        assert stats.effectiveness_score == 80
        assert stats.benchmark_dim_score == 75

    def test_agent_buddy_stats_has_new_fields(self):
        from robothor.engine.buddy import AgentBuddyStats

        stats = AgentBuddyStats(
            agent_id="test",
            stat_date=date(2026, 4, 14),
            effectiveness_score=90,
            benchmark_dim_score=85,
        )
        assert stats.effectiveness_score == 90
        assert stats.benchmark_dim_score == 85

    def test_agent_buddy_stats_defaults(self):
        from robothor.engine.buddy import AgentBuddyStats

        stats = AgentBuddyStats(agent_id="test", stat_date=date(2026, 4, 14))
        assert stats.effectiveness_score == 50
        assert stats.benchmark_dim_score == 50


# ── Per-agent compute uses new dimensions ────────────────────────────


class TestPerAgentComputeNewDimensions:
    """Verify compute_agent_scores populates effectiveness and benchmark scores."""

    @patch("robothor.engine.buddy.BuddyEngine._get_latest_benchmark", return_value=(0.85, None))
    @patch("robothor.engine.buddy.BuddyEngine._get_agent_total_xp", return_value=500)
    @patch("robothor.engine.buddy.BuddyEngine._compute_reliability", return_value=90)
    @patch("robothor.engine.buddy.BuddyEngine._compute_benchmark_score", return_value=85)
    @patch("robothor.engine.buddy.BuddyEngine._compute_effectiveness", return_value=80)
    @patch("robothor.engine.buddy.BuddyEngine._compute_patience", return_value=70)
    @patch("robothor.engine.buddy.BuddyEngine._compute_debugging", return_value=80)
    @patch("robothor.engine.buddy.BuddyEngine._agent_run_count", return_value=10)
    @patch("robothor.db.connection.get_connection")
    def test_uses_new_dimensions(
        self,
        mock_conn,
        mock_runcount,
        mock_dbg,
        mock_pat,
        mock_eff,
        mock_bench_dim,
        mock_rel,
        mock_xp,
        mock_bench,
    ):
        cursor = MagicMock()
        call_idx = [0]
        results = [
            [(5,)],  # tasks_completed
            [(10,)],  # emails_processed
            [(1,)],  # errors_avoided
        ]

        def fetchone():
            idx = call_idx[0]
            call_idx[0] += 1
            return results[idx][0] if idx < len(results) else None

        cursor.fetchone = fetchone
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        stats = engine.compute_agent_scores("test-agent", date(2026, 4, 14))

        assert stats is not None
        assert stats.effectiveness_score == 80
        assert stats.benchmark_dim_score == 85
        assert stats.overall_score > 0
        # Overall should use new formula, not old chaos/wisdom
        # 90*0.25 + 80*0.20 + 70*0.15 + 80*0.25 + 85*0.15 = 81.25 → 81
        assert stats.overall_score == 81


# ── Goal evaluation ──────────────────────────────────────────────────


class TestEvaluateGoals:
    """Tests for _evaluate_goals — per-agent goal attainment scoring."""

    @patch("robothor.db.connection.get_connection")
    def test_all_goals_met(self, mock_conn):
        """All goals met → score = 1.0."""
        cursor = MagicMock()
        # completion_rate query: total=100, completed=98
        cursor.fetchone.return_value = (100, 98)
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        goals = [
            {
                "id": "high-completion",
                "metric": "completion_rate",
                "target": ">0.95",
                "weight": 1.0,
            },
        ]
        score = engine._evaluate_goals("test-agent", goals, date(2026, 4, 14))
        assert score == 1.0

    @patch("robothor.db.connection.get_connection")
    def test_no_goals_met(self, mock_conn):
        """No goals met → score = 0.0."""
        cursor = MagicMock()
        # completion_rate query: total=100, completed=50
        cursor.fetchone.return_value = (100, 50)
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        goals = [
            {
                "id": "high-completion",
                "metric": "completion_rate",
                "target": ">0.95",
                "weight": 1.0,
            },
        ]
        score = engine._evaluate_goals("test-agent", goals, date(2026, 4, 14))
        assert score == 0.0

    @patch("robothor.db.connection.get_connection")
    def test_weighted_partial(self, mock_conn):
        """Mix of met/unmet with different weights."""
        cursor = MagicMock()
        # First call: completion_rate → total=100, completed=98 (meets >0.95)
        # Second call: avg_duration_ms → avg=2000000 (does NOT meet <1800000)
        cursor.fetchone.side_effect = [(100, 98), (2000000.0,)]
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        goals = [
            {
                "id": "high-completion",
                "metric": "completion_rate",
                "target": ">0.95",
                "weight": 2.0,
            },
            {"id": "fast", "metric": "avg_duration_ms", "target": "<1800000", "weight": 1.0},
        ]
        score = engine._evaluate_goals("test-agent", goals, date(2026, 4, 14))
        # Met: weight=2.0, Unmet: weight=1.0. Score = 2.0/3.0 ≈ 0.667
        assert abs(score - 2.0 / 3.0) < 0.01

    def test_empty_goals(self):
        """Empty goals list → score = 1.0 (vacuously true)."""
        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        score = engine._evaluate_goals("test-agent", [], date(2026, 4, 14))
        assert score == 1.0

    @patch("robothor.db.connection.get_connection")
    def test_less_than_operator(self, mock_conn):
        """Target with < operator."""
        cursor = MagicMock()
        cursor.fetchone.return_value = (500000.0,)  # avg_duration_ms = 500s (meets <1800000)
        mock_conn.return_value = _mock_conn_ctx(cursor)

        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        goals = [
            {"id": "fast", "metric": "avg_duration_ms", "target": "<1800000", "weight": 1.0},
        ]
        score = engine._evaluate_goals("test-agent", goals, date(2026, 4, 14))
        assert score == 1.0
