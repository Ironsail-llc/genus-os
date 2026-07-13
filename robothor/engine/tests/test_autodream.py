"""Tests for the autoDream module — opportunistic memory consolidation."""

from __future__ import annotations

import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Cooldown Logic ──────────────────────────────────────────────────────────


class TestCooldown:
    """Tests for is_cooled_down() and timestamp management."""

    @patch("robothor.engine.autodream._get_last_run_ts", return_value=None)
    def test_cooled_down_when_never_run(self, mock_ts):
        from robothor.engine.autodream import is_cooled_down

        assert is_cooled_down() is True

    @patch("robothor.engine.autodream._get_last_run_ts")
    def test_not_cooled_down_when_recent(self, mock_ts):
        mock_ts.return_value = time.time() - 60  # 1 minute ago
        from robothor.engine.autodream import is_cooled_down

        assert is_cooled_down() is False

    @patch("robothor.engine.autodream._get_last_run_ts")
    def test_cooled_down_after_timeout(self, mock_ts):
        mock_ts.return_value = time.time() - 2000  # 33 minutes ago
        from robothor.engine.autodream import is_cooled_down

        assert is_cooled_down() is True

    @patch("robothor.engine.autodream._get_last_run_ts")
    @patch("robothor.engine.autodream.COOLDOWN_SECONDS", 600)
    def test_custom_cooldown(self, mock_ts):
        mock_ts.return_value = time.time() - 500  # 8 min ago, cooldown is 10 min
        from robothor.engine.autodream import is_cooled_down

        assert is_cooled_down() is False


class TestLastRunTimestampWithSource:
    """_get_last_run_ts_with_source: Redis->file read with value validation."""

    @staticmethod
    def _redis(value):
        r = MagicMock()
        r.get.return_value = value
        return r

    @patch("robothor.events.bus._get_redis")
    def test_source_redis(self, mock_redis):
        from robothor.engine.autodream import _get_last_run_ts_with_source

        ts = time.time() - 100
        mock_redis.return_value = self._redis(str(ts))
        val, source = _get_last_run_ts_with_source()
        assert source == "redis"
        assert val is not None and abs(val - ts) < 1

    @patch("robothor.events.bus._get_redis", return_value=None)
    def test_source_file_fallback(self, mock_redis, tmp_path, monkeypatch):
        from robothor.engine import autodream

        ts = time.time() - 100
        f = tmp_path / "last_run"
        f.write_text(str(ts))
        monkeypatch.setattr(autodream, "_FALLBACK_PATH", str(f))
        val, source = autodream._get_last_run_ts_with_source()
        assert source == "file"
        assert val is not None and abs(val - ts) < 1

    @patch("robothor.events.bus._get_redis", return_value=None)
    def test_source_none_when_no_file(self, mock_redis, tmp_path, monkeypatch):
        from robothor.engine import autodream

        monkeypatch.setattr(autodream, "_FALLBACK_PATH", str(tmp_path / "missing"))
        val, source = autodream._get_last_run_ts_with_source()
        assert val is None
        assert source == "none"

    @patch("robothor.events.bus._get_redis")
    def test_rejects_future_redis(self, mock_redis):
        from robothor.engine.autodream import _get_last_run_ts_with_source

        mock_redis.return_value = self._redis(str(time.time() + 10_000))
        val, source = _get_last_run_ts_with_source()
        assert val is None
        assert source == "invalid"

    @patch("robothor.events.bus._get_redis", return_value=None)
    def test_rejects_future_file(self, mock_redis, tmp_path, monkeypatch):
        from robothor.engine import autodream

        f = tmp_path / "last_run"
        f.write_text(str(time.time() + 10_000))
        monkeypatch.setattr(autodream, "_FALLBACK_PATH", str(f))
        val, source = autodream._get_last_run_ts_with_source()
        assert val is None
        assert source == "invalid"

    @patch("robothor.events.bus._get_redis", return_value=None)
    def test_rejects_nan_file(self, mock_redis, tmp_path, monkeypatch):
        from robothor.engine import autodream

        f = tmp_path / "last_run"
        f.write_text("nan")
        monkeypatch.setattr(autodream, "_FALLBACK_PATH", str(f))
        val, source = autodream._get_last_run_ts_with_source()
        assert val is None
        assert source == "invalid"

    @patch("robothor.events.bus._get_redis", return_value=None)
    def test_rejects_negative_inf_file(self, mock_redis, tmp_path, monkeypatch):
        from robothor.engine import autodream

        f = tmp_path / "last_run"
        f.write_text("-inf")
        monkeypatch.setattr(autodream, "_FALLBACK_PATH", str(f))
        val, source = autodream._get_last_run_ts_with_source()
        assert val is None
        assert source == "invalid"

    @patch("robothor.events.bus._get_redis")
    def test_accepts_within_skew_tolerance(self, mock_redis):
        from robothor.engine.autodream import _get_last_run_ts_with_source

        # +100s is within the 300s skew tolerance -> accepted.
        mock_redis.return_value = self._redis(str(time.time() + 100))
        val, source = _get_last_run_ts_with_source()
        assert source == "redis"
        assert val is not None

    @patch("robothor.events.bus._get_redis", return_value=None)
    def test_file_older_than_max_age_ignored(self, mock_redis, tmp_path, monkeypatch):
        import os

        from robothor.engine import autodream

        f = tmp_path / "last_run"
        old = time.time() - 100_000  # ~27.7h, beyond the ~25h default max age
        f.write_text(str(old))
        os.utime(f, (old, old))
        monkeypatch.setattr(autodream, "_FALLBACK_PATH", str(f))
        val, source = autodream._get_last_run_ts_with_source()
        assert val is None
        assert source == "none"

    @patch(
        "robothor.engine.autodream._get_last_run_ts_with_source",
        return_value=(123.0, "redis"),
    )
    def test_wrapper_returns_float_only(self, mock_src):
        from robothor.engine.autodream import _get_last_run_ts

        assert _get_last_run_ts() == 123.0

    @patch(
        "robothor.engine.autodream._get_last_run_ts_with_source",
        return_value=(None, "invalid"),
    )
    def test_is_cooled_down_true_when_timestamp_invalid(self, mock_src):
        # A future/corrupt timestamp is rejected -> treated as never-run ->
        # a run is allowed (self-heal: the run overwrites the bad value).
        from robothor.engine.autodream import is_cooled_down

        assert is_cooled_down() is True


# ── Quiet Hours Detection ───────────────────────────────────────────────────


class TestQuietHours:
    """Tests for _is_quiet_hours()."""

    @patch("robothor.engine.autodream.datetime")
    def test_quiet_hours_late_night(self, mock_dt):
        from robothor.engine.autodream import _is_quiet_hours

        mock_now = MagicMock()
        mock_now.hour = 23  # 11 PM
        mock_dt.now.return_value = mock_now
        assert _is_quiet_hours() is True

    @patch("robothor.engine.autodream.datetime")
    def test_quiet_hours_early_morning(self, mock_dt):
        from robothor.engine.autodream import _is_quiet_hours

        mock_now = MagicMock()
        mock_now.hour = 3  # 3 AM
        mock_dt.now.return_value = mock_now
        assert _is_quiet_hours() is True

    @patch("robothor.engine.autodream.datetime")
    def test_not_quiet_hours_daytime(self, mock_dt):
        from robothor.engine.autodream import _is_quiet_hours

        mock_now = MagicMock()
        mock_now.hour = 14  # 2 PM
        mock_dt.now.return_value = mock_now
        assert _is_quiet_hours() is False


# ── run_autodream() ─────────────────────────────────────────────────────────


class TestRunAutodream:
    """Tests for the main run_autodream() orchestrator."""

    @pytest.mark.asyncio
    @patch("robothor.engine.autodream.release_lock")
    @patch("robothor.engine.autodream.try_acquire_lock", return_value=True)
    @patch("robothor.engine.autodream._update_memory_block")
    @patch("robothor.engine.autodream._record_run")
    @patch("robothor.engine.autodream._set_last_run_ts")
    @patch("robothor.engine.autodream._publish_event")
    @patch("robothor.engine.autodream._is_quiet_hours", return_value=False)
    @patch("robothor.engine.autodream.discover_cross_domain_insights", new_callable=AsyncMock)
    @patch("robothor.engine.autodream.prune_low_quality_facts", new_callable=AsyncMock)
    @patch("robothor.engine.autodream.run_intraday_consolidation", new_callable=AsyncMock)
    async def test_idle_mode_runs_lightweight(
        self,
        mock_consol,
        mock_prune,
        mock_insights,
        mock_quiet,
        mock_pub,
        mock_ts,
        mock_rec,
        mock_block,
        mock_lock,
        mock_unlock,
    ):
        mock_consol.return_value = {"skipped": False, "consolidation_groups": 2}
        mock_prune.return_value = {"total_pruned": 3}
        mock_insights.return_value = [{"insight_text": "test insight"}]

        from robothor.engine.autodream import run_autodream

        result = await run_autodream(mode="idle")

        assert result["mode"] == "idle"
        assert result["facts_consolidated"] == 2
        assert result["facts_pruned"] == 3
        assert result["insights_discovered"] == 1
        mock_consol.assert_called_once_with(threshold=3)
        mock_prune.assert_called_once()
        mock_insights.assert_called_once_with(hours_back=72)
        mock_ts.assert_called_once()
        mock_rec.assert_called_once()

    @pytest.mark.asyncio
    @patch("robothor.engine.autodream.release_lock")
    @patch("robothor.engine.autodream.try_acquire_lock", return_value=True)
    @patch("robothor.engine.autodream._update_memory_block")
    @patch("robothor.engine.autodream._record_run")
    @patch("robothor.engine.autodream._set_last_run_ts")
    @patch("robothor.engine.autodream._publish_event")
    @patch("robothor.engine.autodream._is_quiet_hours", return_value=False)
    @patch("robothor.engine.autodream.discover_cross_domain_insights", new_callable=AsyncMock)
    @patch("robothor.engine.autodream.prune_low_quality_facts", new_callable=AsyncMock)
    @patch("robothor.engine.autodream.run_intraday_consolidation", new_callable=AsyncMock)
    async def test_idle_skips_consolidation_when_below_threshold(
        self,
        mock_consol,
        mock_prune,
        mock_insights,
        mock_quiet,
        mock_pub,
        mock_ts,
        mock_rec,
        mock_block,
        mock_lock,
        mock_unlock,
    ):
        mock_consol.return_value = {"skipped": True, "unconsolidated_count": 1}
        mock_prune.return_value = {"total_pruned": 0}
        mock_insights.return_value = []

        from robothor.engine.autodream import run_autodream

        result = await run_autodream(mode="idle")

        assert result["facts_consolidated"] == 0
        assert result["facts_pruned"] == 0
        assert result["insights_discovered"] == 0

    @pytest.mark.asyncio
    @patch("robothor.engine.autodream.release_lock")
    @patch("robothor.engine.autodream.try_acquire_lock", return_value=True)
    @patch("robothor.engine.autodream._update_memory_block")
    @patch("robothor.engine.autodream._record_run")
    @patch("robothor.engine.autodream._set_last_run_ts")
    @patch("robothor.engine.autodream._publish_event")
    @patch("robothor.engine.autodream._is_quiet_hours", return_value=True)
    @patch("robothor.engine.autodream.run_lifecycle_maintenance", new_callable=AsyncMock)
    async def test_idle_upgrades_to_deep_during_quiet_hours(
        self,
        mock_maint,
        mock_quiet,
        mock_pub,
        mock_ts,
        mock_rec,
        mock_block,
        mock_lock,
        mock_unlock,
    ):
        mock_maint.return_value = {
            "consolidation_groups": 5,
            "total_pruned": 10,
            "insights": [{"insight_text": "a"}, {"insight_text": "b"}],
            "facts_scored": 20,
        }

        from robothor.engine.autodream import run_autodream

        result = await run_autodream(mode="idle")

        assert result["mode"] == "deep"
        assert result["facts_consolidated"] == 5
        assert result["facts_pruned"] == 10
        assert result["insights_discovered"] == 2
        assert result["importance_scores_updated"] == 20
        mock_maint.assert_called_once()

    @pytest.mark.asyncio
    @patch("robothor.engine.autodream.release_lock")
    @patch("robothor.engine.autodream.try_acquire_lock", return_value=True)
    @patch("robothor.engine.autodream._update_memory_block")
    @patch("robothor.engine.autodream._record_run")
    @patch("robothor.engine.autodream._set_last_run_ts")
    @patch("robothor.engine.autodream._publish_event")
    @patch("robothor.engine.autodream._is_quiet_hours", return_value=False)
    @patch("robothor.engine.autodream.run_lifecycle_maintenance", new_callable=AsyncMock)
    async def test_deep_mode_runs_full_lifecycle(
        self,
        mock_maint,
        mock_quiet,
        mock_pub,
        mock_ts,
        mock_rec,
        mock_block,
        mock_lock,
        mock_unlock,
    ):
        mock_maint.return_value = {
            "consolidation_groups": 3,
            "total_pruned": 7,
            "insights": [],
            "facts_scored": 15,
        }

        from robothor.engine.autodream import run_autodream

        result = await run_autodream(mode="deep")

        assert result["mode"] == "deep"
        mock_maint.assert_called_once()

    @pytest.mark.asyncio
    @patch("robothor.engine.autodream.release_lock")
    @patch("robothor.engine.autodream.try_acquire_lock", return_value=True)
    @patch("robothor.engine.autodream._update_memory_block")
    @patch("robothor.engine.autodream._record_run")
    @patch("robothor.engine.autodream._set_last_run_ts")
    @patch("robothor.engine.autodream._publish_event")
    @patch("robothor.engine.autodream._is_quiet_hours", return_value=False)
    @patch("robothor.engine.autodream.discover_cross_domain_insights", new_callable=AsyncMock)
    @patch("robothor.engine.autodream.prune_low_quality_facts", new_callable=AsyncMock)
    @patch("robothor.engine.autodream.run_intraday_consolidation", new_callable=AsyncMock)
    async def test_post_stall_mode(
        self,
        mock_consol,
        mock_prune,
        mock_insights,
        mock_quiet,
        mock_pub,
        mock_ts,
        mock_rec,
        mock_block,
        mock_lock,
        mock_unlock,
    ):
        mock_consol.return_value = {"skipped": False, "consolidation_groups": 1}
        mock_prune.return_value = {"total_pruned": 0}
        mock_insights.return_value = []

        from robothor.engine.autodream import run_autodream

        result = await run_autodream(mode="post_stall")

        assert result["mode"] == "post_stall"

    @pytest.mark.asyncio
    @patch("robothor.engine.autodream.release_lock")
    @patch("robothor.engine.autodream.try_acquire_lock", return_value=True)
    @patch("robothor.engine.autodream._update_memory_block")
    @patch("robothor.engine.autodream._record_run")
    @patch("robothor.engine.autodream._set_last_run_ts")
    @patch("robothor.engine.autodream._publish_event")
    @patch("robothor.engine.autodream._is_quiet_hours", return_value=False)
    @patch("robothor.engine.autodream.run_intraday_consolidation", new_callable=AsyncMock)
    async def test_handles_lifecycle_errors_gracefully(
        self,
        mock_consol,
        mock_quiet,
        mock_pub,
        mock_ts,
        mock_rec,
        mock_block,
        mock_lock,
        mock_unlock,
    ):
        mock_consol.side_effect = RuntimeError("DB connection failed")

        from robothor.engine.autodream import run_autodream

        await run_autodream(mode="idle")

        # Should still complete, record the error, and set timestamp
        mock_ts.assert_called_once()
        mock_rec.assert_called_once()
        # Error should be recorded
        call_args = mock_rec.call_args
        assert call_args[1].get("error") or (len(call_args[0]) >= 5 and call_args[0][4] is not None)

    @pytest.mark.asyncio
    @patch("robothor.engine.autodream.release_lock")
    @patch("robothor.engine.autodream.try_acquire_lock", return_value=True)
    @patch("robothor.engine.autodream._update_memory_block")
    @patch("robothor.engine.autodream._record_run")
    @patch("robothor.engine.autodream._set_last_run_ts")
    @patch("robothor.engine.autodream._publish_event")
    @patch("robothor.engine.autodream._is_quiet_hours", return_value=False)
    @patch("robothor.engine.autodream.discover_cross_domain_insights", new_callable=AsyncMock)
    @patch("robothor.engine.autodream.prune_low_quality_facts", new_callable=AsyncMock)
    @patch("robothor.engine.autodream.run_intraday_consolidation", new_callable=AsyncMock)
    async def test_publishes_event_on_completion(
        self,
        mock_consol,
        mock_prune,
        mock_insights,
        mock_quiet,
        mock_pub,
        mock_ts,
        mock_rec,
        mock_block,
        mock_lock,
        mock_unlock,
    ):
        mock_consol.return_value = {"skipped": True}
        mock_prune.return_value = {"total_pruned": 0}
        mock_insights.return_value = []

        from robothor.engine.autodream import run_autodream

        await run_autodream(mode="idle")

        mock_pub.assert_called_once()
        event_type, data = mock_pub.call_args[0]
        assert event_type == "autodream.complete"
        assert "run_id" in data
        assert "duration_ms" in data


# ── Daemon Loop Integration ─────────────────────────────────────────────────


class TestAutodreamLoop:
    """Tests for the _autodream_loop integration in daemon.py."""

    @pytest.fixture(autouse=True)
    def _reset_defer_state(self):
        """Reset the in-process loop globals so tests don't bleed into each other."""
        from robothor.engine import daemon

        daemon._autodream_defer_started_at = None
        daemon._autodream_stale_alerted = False
        yield
        daemon._autodream_defer_started_at = None
        daemon._autodream_stale_alerted = False

    @pytest.mark.asyncio
    @patch("robothor.engine.dedup.running_agents", return_value=set())
    @patch("robothor.engine.autodream.is_cooled_down", return_value=True)
    @patch("robothor.engine.autodream.run_autodream", new_callable=AsyncMock)
    async def test_loop_triggers_on_idle(self, mock_dream, mock_cool, mock_agents):
        """Verify the daemon loop calls run_autodream when idle and cooled down."""
        from robothor.engine.daemon import _autodream_loop

        call_count = 0

        async def counting_dream(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise asyncio.CancelledError  # stop the loop after first call

        mock_dream.side_effect = counting_dream

        with patch("robothor.engine.daemon.asyncio.sleep", new_callable=AsyncMock):
            with contextlib.suppress(asyncio.CancelledError):
                await _autodream_loop()

        assert call_count >= 1

    @pytest.mark.asyncio
    @patch("robothor.engine.dedup.running_agents", return_value={"email-classifier"})
    @patch("robothor.engine.autodream.is_cooled_down", return_value=True)
    @patch("robothor.engine.autodream.run_autodream", new_callable=AsyncMock)
    async def test_loop_skips_when_agents_active(self, mock_dream, mock_cool, mock_agents):
        """Verify the loop does NOT trigger when agents are running."""
        from robothor.engine.daemon import _autodream_loop

        iteration = 0

        async def counting_sleep(seconds):
            nonlocal iteration
            iteration += 1
            if iteration > 3:
                raise asyncio.CancelledError

        with patch("robothor.engine.daemon.asyncio.sleep", side_effect=counting_sleep):
            with contextlib.suppress(asyncio.CancelledError):
                await _autodream_loop()

        mock_dream.assert_not_called()

    @pytest.mark.asyncio
    @patch("robothor.engine.dedup.running_agents", return_value=set())
    @patch("robothor.engine.autodream.is_cooled_down", return_value=False)
    @patch("robothor.engine.autodream.run_autodream", new_callable=AsyncMock)
    async def test_loop_skips_when_not_cooled_down(self, mock_dream, mock_cool, mock_agents):
        """Verify the loop respects cooldown."""
        iteration = 0

        async def counting_sleep(seconds):
            nonlocal iteration
            iteration += 1
            if iteration > 3:
                raise asyncio.CancelledError

        with patch("robothor.engine.daemon.asyncio.sleep", side_effect=counting_sleep):
            from robothor.engine.daemon import _autodream_loop

            with contextlib.suppress(asyncio.CancelledError):
                await _autodream_loop()

        mock_dream.assert_not_called()


class TestMaxDeferForcesDeep:
    """Under sustained load, the MAX_DEFER force-run must use DEEP mode (full
    lifecycle maintenance), not post_stall — the reviewer's HIGH finding."""

    @pytest.mark.asyncio
    async def test_maxdefer_force_uses_deep_not_post_stall(self, monkeypatch):
        import asyncio as _aio
        import contextlib

        import robothor.engine.autodream as ad
        import robothor.engine.dedup as dedup
        from robothor.engine import daemon

        calls = []

        async def _fake_run(mode=None, **k):
            calls.append(mode)
            return {}

        monkeypatch.setattr(ad, "run_autodream", _fake_run)
        monkeypatch.setattr(ad, "is_cooled_down", lambda: False)  # not cooled → defer path
        monkeypatch.setattr(dedup, "running_agents", lambda: {"main"})  # busy
        monkeypatch.setattr(
            daemon,
            "_autodream_defer_decision",
            lambda *a: {"force": True, "defer_started_at": 0.0, "deferred_for": 999999.0},
        )

        async def _sleep(_s):
            if calls:  # break out once the forced run has happened
                raise _aio.CancelledError

        monkeypatch.setattr(daemon.asyncio, "sleep", _sleep)
        with contextlib.suppress(_aio.CancelledError):
            await daemon._autodream_loop()
        assert calls and calls[0] == "deep", f"forced run used {calls}, expected deep"


class TestFallbackPathSecurity:
    """The last-run fallback must not live in world-writable /tmp and must not
    follow a symlink (CWE-59)."""

    def test_default_fallback_not_in_tmp(self):
        from robothor.engine import autodream

        path = autodream._default_fallback_path()
        assert not path.startswith("/tmp"), path
        assert ".robothor" in path

    def test_set_last_run_does_not_follow_symlink(self, tmp_path, monkeypatch):
        from robothor.engine import autodream

        monkeypatch.setattr("robothor.events.bus._get_redis", lambda: None, raising=False)
        # First a normal write works.
        target = tmp_path / "ts"
        monkeypatch.setattr(autodream, "_FALLBACK_PATH", str(target))
        autodream._set_last_run_ts()
        assert target.exists() and float(target.read_text()) > 0

        # A symlink pre-planted at the path must NOT be followed → victim intact.
        victim = tmp_path / "victim"
        victim.write_text("SECRET")
        link = tmp_path / "link"
        link.symlink_to(victim)
        monkeypatch.setattr(autodream, "_FALLBACK_PATH", str(link))
        autodream._set_last_run_ts()  # must not raise
        assert victim.read_text() == "SECRET", "symlink target was clobbered"
