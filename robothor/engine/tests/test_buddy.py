"""Tests for the Buddy gamification engine."""

from __future__ import annotations

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
        from robothor.engine.buddy_hooks import _on_agent_end

        result = _on_agent_end({"agent_id": "main", "status": "completed", "run_id": "123"})

        assert result["action"] == "allow"
        mock_inc.assert_called_once()

    @patch("robothor.engine.buddy.BuddyEngine.increment_task_count")
    def test_on_agent_end_failed_skips(self, mock_inc):
        from robothor.engine.buddy_hooks import _on_agent_end

        result = _on_agent_end({"agent_id": "main", "status": "failed", "run_id": "123"})

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

    @patch("robothor.memory.blocks.read_block")
    def test_buddy_status_context_main(self, mock_block):
        mock_block.return_value = {"content": "Level 5 Flame (2000 XP)"}
        from robothor.engine.models import AgentConfig
        from robothor.engine.warmup import _buddy_status_context

        config = AgentConfig(id="main", name="Main")
        result = _buddy_status_context(config)
        assert result is not None
        assert result.startswith("[BUDDY]")
        assert "Level 5" in result

    def test_buddy_status_context_non_main(self):
        from robothor.engine.models import AgentConfig
        from robothor.engine.warmup import _buddy_status_context

        config = AgentConfig(id="email-classifier", name="Email Classifier")
        result = _buddy_status_context(config)
        assert result is None

    @patch("robothor.memory.blocks.read_block")
    def test_buddy_status_context_empty_block(self, mock_block):
        mock_block.return_value = {"content": ""}
        from robothor.engine.models import AgentConfig
        from robothor.engine.warmup import _buddy_status_context

        config = AgentConfig(id="main", name="Main")
        result = _buddy_status_context(config)
        assert result is None
