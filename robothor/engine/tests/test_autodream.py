"""Tests for the autoDream module — opportunistic memory consolidation."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _cfg(tenant: str = "default", chat: str = "123"):
    """Build a minimal EngineConfig stand-in for autoDream gating tests."""
    c = MagicMock()
    c.tenant_id = tenant
    c.default_chat_id = chat
    return c


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

    @pytest.mark.asyncio
    @patch("robothor.engine.autodream._get_last_run_ts", return_value=None)
    @patch("robothor.engine.dedup.running_agents", return_value=set())
    @patch("robothor.engine.autodream.is_cooled_down", return_value=True)
    @patch("robothor.engine.autodream.run_autodream", new_callable=AsyncMock)
    async def test_loop_triggers_on_idle(self, mock_dream, mock_cool, mock_agents, mock_ts):
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
                await _autodream_loop(_cfg())

        assert call_count >= 1

    @pytest.mark.asyncio
    @patch("robothor.engine.autodream._get_last_run_ts", return_value=None)
    @patch("robothor.engine.dedup.running_agents", return_value={"email-classifier"})
    @patch("robothor.engine.autodream.is_cooled_down", return_value=True)
    @patch("robothor.engine.autodream.run_autodream", new_callable=AsyncMock)
    async def test_loop_skips_when_agents_active(self, mock_dream, mock_cool, mock_agents, mock_ts):
        """Verify the loop does NOT trigger when agents are running (within defer ceiling)."""
        from robothor.engine.daemon import _autodream_loop

        iteration = 0

        async def counting_sleep(seconds):
            nonlocal iteration
            iteration += 1
            if iteration > 3:
                raise asyncio.CancelledError

        with patch("robothor.engine.daemon.asyncio.sleep", side_effect=counting_sleep):
            with contextlib.suppress(asyncio.CancelledError):
                await _autodream_loop(_cfg())

        mock_dream.assert_not_called()

    @pytest.mark.asyncio
    @patch("robothor.engine.autodream._get_last_run_ts", return_value=None)
    @patch("robothor.engine.dedup.running_agents", return_value=set())
    @patch("robothor.engine.autodream.is_cooled_down", return_value=False)
    @patch("robothor.engine.autodream.run_autodream", new_callable=AsyncMock)
    async def test_loop_skips_when_not_cooled_down(
        self, mock_dream, mock_cool, mock_agents, mock_ts
    ):
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
                await _autodream_loop(_cfg())

        mock_dream.assert_not_called()

    @pytest.mark.asyncio
    @patch("robothor.engine.autodream.run_autodream", new_callable=AsyncMock)
    async def test_loop_disabled_for_child_tenant(self, mock_dream):
        """A child-tenant daemon (e.g. Delphi) must NOT run autoDream — it returns at once."""
        from robothor.engine.daemon import _autodream_loop

        # No sleep patch needed: a disabled loop returns immediately without iterating.
        await _autodream_loop(_cfg(tenant="delphi"))
        mock_dream.assert_not_called()


# ── Tenant Gating ───────────────────────────────────────────────────────────


class TestAutodreamEnabled:
    """Tests for _autodream_enabled() — autoDream is a main/root-engine concern."""

    def test_enabled_for_main_tenant(self):
        from robothor.engine.autodream import _autodream_enabled

        assert _autodream_enabled(_cfg(tenant="default")) is True

    def test_disabled_for_child_tenant(self):
        from robothor.engine.autodream import _autodream_enabled

        assert _autodream_enabled(_cfg(tenant="delphi")) is False

    @patch.dict(os.environ, {"AUTODREAM_ENABLED": "1"})
    def test_env_override_enables_child_tenant(self):
        from robothor.engine.autodream import _autodream_enabled

        assert _autodream_enabled(_cfg(tenant="delphi")) is True

    @patch.dict(os.environ, {"AUTODREAM_ENABLED": "0"})
    def test_env_override_disables_main_tenant(self):
        from robothor.engine.autodream import _autodream_enabled

        assert _autodream_enabled(_cfg(tenant="default")) is False


# ── Watchdog Staleness Alert ─────────────────────────────────────────────────


class TestStalenessAlert:
    """Tests for daemon.autodream_staleness_alert() — alert only on GENUINE failure."""

    def test_alert_threshold_above_max_defer(self):
        from robothor.engine import autodream

        # The alert must fire strictly above the guaranteed-run ceiling, which is
        # itself above the per-run cooldown. Otherwise intentional deferral alarms.
        assert (
            autodream.WATCHDOG_ALERT_THRESHOLD
            > autodream.MAX_DEFER_SECONDS
            > autodream.COOLDOWN_SECONDS
        )

    @patch("robothor.engine.autodream._get_last_run_ts")
    def test_no_alert_during_deferral_window(self, mock_ts):
        from robothor.engine.autodream import MAX_DEFER_SECONDS, WATCHDOG_ALERT_THRESHOLD
        from robothor.engine.daemon import autodream_staleness_alert

        # Staleness between the defer ceiling and the alert threshold = intentional defer.
        mid = (MAX_DEFER_SECONDS + WATCHDOG_ALERT_THRESHOLD) / 2
        mock_ts.return_value = time.time() - mid
        assert autodream_staleness_alert(_cfg()) is None

    @patch("robothor.engine.autodream._get_last_run_ts")
    def test_alerts_on_genuine_failure(self, mock_ts):
        from robothor.engine.autodream import WATCHDOG_ALERT_THRESHOLD
        from robothor.engine.daemon import autodream_staleness_alert

        mock_ts.return_value = time.time() - (WATCHDOG_ALERT_THRESHOLD + 3600)
        msg = autodream_staleness_alert(_cfg())
        assert msg is not None
        assert "has not run" in msg

    @patch("robothor.engine.autodream._get_last_run_ts")
    def test_skips_alert_when_disabled_for_child_tenant(self, mock_ts):
        from robothor.engine.daemon import autodream_staleness_alert

        # Even at 10h staleness, a child-tenant daemon must not alert.
        mock_ts.return_value = time.time() - (10 * 3600)
        assert autodream_staleness_alert(_cfg(tenant="delphi")) is None


# ── Lock TTL ─────────────────────────────────────────────────────────────────


class TestLockTTL:
    """The autoDream lock must outlive a deep run so it can't expire mid-pass."""

    def test_lock_ttl_exceeds_max_run_and_decoupled_from_cooldown(self):
        from robothor.engine import autodream

        # A deep run can exceed COOLDOWN (importance scoring alone budgets 600s, one
        # of six steps). The lock TTL must be independent of, and larger than, cooldown.
        assert autodream.AUTODREAM_LOCK_TTL >= 1800
        assert autodream.AUTODREAM_LOCK_TTL != autodream.COOLDOWN_SECONDS

    @patch("robothor.events.bus._get_redis")
    def test_try_acquire_lock_uses_lock_ttl(self, mock_get_redis):
        from robothor.engine import autodream

        r = MagicMock()
        r.set.return_value = True
        mock_get_redis.return_value = r

        autodream.try_acquire_lock("run-1")

        _, kwargs = r.set.call_args
        assert kwargs["ex"] == autodream.AUTODREAM_LOCK_TTL

    @patch("robothor.events.bus._get_redis")
    def test_release_lock_declines_foreign_lock(self, mock_get_redis):
        from robothor.engine import autodream

        r = MagicMock()
        r.get.return_value = "other-run-id"
        mock_get_redis.return_value = r

        autodream.release_lock("my-run-id")

        r.delete.assert_not_called()
