"""Tests for daemon.py watchdog/autoDream-loop helpers and _sd_notify()."""

import asyncio
import contextlib
import socket
import time
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import daemon
from robothor.engine.daemon import (
    _autodream_defer_decision,
    _autodream_staleness_decision,
    _resolve_last_run,
    _sd_notify,
)


@pytest.fixture(autouse=True)
def _reset_autodream_globals():
    """Reset the in-process loop/watchdog globals so tests don't bleed."""
    daemon._autodream_defer_started_at = None
    daemon._autodream_stale_alerted = False
    yield
    daemon._autodream_defer_started_at = None
    daemon._autodream_stale_alerted = False


class TestSdNotify:
    def test_noop_without_notify_socket(self, monkeypatch):
        """_sd_notify does nothing when NOTIFY_SOCKET is not set."""
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        _sd_notify("READY=1")  # Should not raise

    def test_sends_to_real_socket(self, monkeypatch, tmp_path):
        """_sd_notify sends data to a real Unix datagram socket."""
        sock_path = str(tmp_path / "notify.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(sock_path)
        try:
            monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
            _sd_notify("WATCHDOG=1")
            data = server.recv(256)
            assert data == b"WATCHDOG=1"
        finally:
            server.close()

    def test_handles_bad_socket_path(self, monkeypatch):
        """_sd_notify handles unreachable socket without raising."""
        monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/path/notify.sock")
        _sd_notify("READY=1")  # Should not raise

    def test_handles_abstract_socket(self, monkeypatch):
        """_sd_notify correctly handles abstract socket addresses (@ prefix)."""
        # Create an abstract socket
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind("\0test_sd_notify_abstract")
        try:
            monkeypatch.setenv("NOTIFY_SOCKET", "@test_sd_notify_abstract")
            _sd_notify("READY=1")
            data = server.recv(256)
            assert data == b"READY=1"
        finally:
            server.close()


class TestAutodreamStalenessDecision:
    MAX_DEFER = 4 * 3600
    COOLDOWN = 1800

    def _decide(self, staleness, already_alerted):
        return _autodream_staleness_decision(
            staleness, already_alerted, self.MAX_DEFER, self.COOLDOWN
        )

    def test_none_staleness_resets_and_silent(self):
        assert self._decide(None, True) == {"alert": False, "warn": False, "reset": True}

    def test_below_warn_threshold_resets(self):
        d = self._decide(self.COOLDOWN * 2, True)  # 1h, below 1.5h warn
        assert d == {"alert": False, "warn": False, "reset": True}

    def test_warn_band_logs_only(self):
        d = self._decide(self.COOLDOWN * 4, False)  # 2h, between warn and alert
        assert d["warn"] is True
        assert d["alert"] is False
        assert d["reset"] is False

    def test_alert_first_time(self):
        d = self._decide(self.MAX_DEFER + self.COOLDOWN + 10, False)  # past 4.5h
        assert d["alert"] is True
        assert d["reset"] is False

    def test_alert_debounced_when_already_alerted(self):
        d = self._decide(self.MAX_DEFER + self.COOLDOWN + 10, True)
        assert d["alert"] is False

    def test_alert_threshold_strictly_after_force_window(self):
        # The page must only fire after the loop's own force ceiling, never before.
        assert self.MAX_DEFER + self.COOLDOWN > self.MAX_DEFER


class TestAutodreamDeferDecision:
    MAX_DEFER = 4 * 3600

    def test_not_busy_resets_tracker(self):
        d = _autodream_defer_decision(False, 1000.0, 500.0, self.MAX_DEFER)
        assert d == {"force": False, "defer_started_at": None, "deferred_for": 0.0}

    def test_busy_first_time_sets_start(self):
        d = _autodream_defer_decision(True, 1000.0, None, self.MAX_DEFER)
        assert d["defer_started_at"] == 1000.0
        assert d["force"] is False

    def test_busy_accumulates_below_ceiling(self):
        d = _autodream_defer_decision(True, 1100.0, 1000.0, self.MAX_DEFER)
        assert d["defer_started_at"] == 1000.0
        assert d["force"] is False

    def test_busy_force_at_ceiling(self):
        d = _autodream_defer_decision(True, 1000.0 + self.MAX_DEFER + 1, 1000.0, self.MAX_DEFER)
        assert d["force"] is True

    def test_idle_gap_resets_continuous_clock(self):
        d1 = _autodream_defer_decision(True, 1000.0, None, self.MAX_DEFER)
        assert d1["defer_started_at"] == 1000.0
        d2 = _autodream_defer_decision(False, 2000.0, d1["defer_started_at"], self.MAX_DEFER)
        assert d2["defer_started_at"] is None
        # Busy again starts a *fresh* streak, not the stale one.
        d3 = _autodream_defer_decision(True, 3000.0, d2["defer_started_at"], self.MAX_DEFER)
        assert d3["defer_started_at"] == 3000.0
        assert d3["force"] is False


class TestResolveLastRun:
    @patch(
        "robothor.engine.autodream._get_last_run_ts_with_source",
        return_value=(123.0, "redis"),
    )
    def test_uses_real_source(self, mock_src):
        ts, source = _resolve_last_run()
        assert ts == 123.0
        assert source == "redis"  # no mislabel

    @patch(
        "robothor.engine.autodream._get_last_run_ts_with_source",
        return_value=(None, "invalid"),
    )
    def test_anchors_to_daemon_boot(self, mock_src, monkeypatch):
        monkeypatch.setattr(daemon, "_DAEMON_START_TS", "2026-06-06T00:00:00+00:00")
        ts, source = _resolve_last_run()
        assert ts is not None
        assert source == "daemon boot"

    @patch(
        "robothor.engine.autodream._get_last_run_ts_with_source",
        return_value=(None, "none"),
    )
    def test_unknown_without_anchor(self, mock_src, monkeypatch):
        monkeypatch.setattr(daemon, "_DAEMON_START_TS", None)
        ts, source = _resolve_last_run()
        assert ts is None
        assert source == "unknown"


class TestAutodreamLoopForceBackoff:
    """The max-defer force path must not busy-loop when the lock is held."""

    @pytest.mark.asyncio
    @patch("robothor.engine.dedup.running_agents", return_value={"agent-1"})
    @patch("robothor.engine.autodream.is_cooled_down", return_value=False)
    @patch("robothor.engine.autodream.run_autodream", new_callable=AsyncMock)
    async def test_lock_held_skip_backs_off_and_keeps_tracker(
        self, mock_dream, mock_cool, mock_agents, monkeypatch
    ):
        from robothor.engine.daemon import _autodream_loop

        past = time.time() - (daemon._AUTODREAM_MAX_DEFER_SECONDS + 100)
        monkeypatch.setattr(daemon, "_autodream_defer_started_at", past)
        mock_dream.return_value = {"skipped": True, "reason": "lock_held"}

        sleeps: list[float] = []

        async def recording_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:  # top-of-loop sleep + the back-off sleep
                raise asyncio.CancelledError

        with patch("robothor.engine.daemon.asyncio.sleep", side_effect=recording_sleep):
            with contextlib.suppress(asyncio.CancelledError):
                await _autodream_loop()

        mock_dream.assert_called_once()
        # Backed off ~10 min instead of re-firing at the 60s cadence.
        assert daemon._AUTODREAM_FORCE_BACKOFF_SECONDS in sleeps
        # Defer streak preserved (self-heals when the holding run finishes).
        assert daemon._autodream_defer_started_at == past

    @pytest.mark.asyncio
    @patch("robothor.engine.dedup.running_agents", return_value={"agent-1"})
    @patch("robothor.engine.autodream.is_cooled_down", return_value=False)
    @patch("robothor.engine.autodream.run_autodream", new_callable=AsyncMock)
    async def test_force_run_success_resets_tracker(
        self, mock_dream, mock_cool, mock_agents, monkeypatch
    ):
        from robothor.engine.daemon import _autodream_loop

        past = time.time() - (daemon._AUTODREAM_MAX_DEFER_SECONDS + 100)
        monkeypatch.setattr(daemon, "_autodream_defer_started_at", past)
        mock_dream.return_value = {"skipped": False}

        calls = 0

        async def stopping_sleep(seconds):
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise asyncio.CancelledError

        with patch("robothor.engine.daemon.asyncio.sleep", side_effect=stopping_sleep):
            with contextlib.suppress(asyncio.CancelledError):
                await _autodream_loop()

        mock_dream.assert_called_once()
        assert daemon._autodream_defer_started_at is None  # reset after a real run
