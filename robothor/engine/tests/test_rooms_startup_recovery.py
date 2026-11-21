"""Workspace editions orphaned by the previous engine are freed at start.

A ``building`` edition from before this engine started has no run behind it.
``robothor.rooms.recovery`` frees it when it knows the boot instant, so that
instant must reach it in the daemon's real order: settings are cached first
(``install_lane_executor``), and the boot timestamp is written after. A copy
read from cached settings would therefore always be empty.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import daemon
from robothor.rooms import recovery


@pytest.fixture
def fresh_boot(monkeypatch):
    monkeypatch.setattr(daemon, "_DAEMON_START_TS", None)
    monkeypatch.delenv("ROBOTHOR_DAEMON_START_TS", raising=False)
    monkeypatch.setattr(recovery, "_booted_at", None, raising=False)


@pytest.mark.usefixtures("fresh_boot")
def test_the_boot_instant_reaches_recovery_in_the_daemons_real_order():
    from robothor.settings import get_settings, reset_settings

    reset_settings()
    assert get_settings().engine.daemon_start_ts == ""  # cached before the boot is written

    daemon._set_daemon_start_ts()

    assert daemon._DAEMON_START_TS
    assert recovery._boot() == datetime.fromisoformat(daemon._DAEMON_START_TS)


@pytest.mark.usefixtures("fresh_boot")
async def test_engine_start_sweeps_the_tenants_orphaned_editions():
    daemon._set_daemon_start_ts()
    config = MagicMock()
    config.tenant_id = "test-tenant"
    reap = MagicMock(return_value=[])

    with (
        patch.object(daemon, "resume_interrupted_runs", AsyncMock(return_value=0)),
        patch.object(daemon, "_cleanup_stale_runs", return_value=0),
        patch("robothor.engine.repair_recovery.recover", AsyncMock()),
        patch("robothor.rooms.recovery.reap_stale_editions", reap),
    ):
        await daemon._recover_interrupted_work(MagicMock(), config)

    reap.assert_called_once()
    args, kwargs = reap.call_args
    assert args == ("test-tenant",)
    assert kwargs["booted_at"] == datetime.fromisoformat(daemon._DAEMON_START_TS)


@pytest.mark.usefixtures("fresh_boot")
async def test_a_failed_sweep_does_not_stop_the_engine():
    config = MagicMock()
    config.tenant_id = "test-tenant"
    recover = AsyncMock()

    with (
        patch.object(daemon, "resume_interrupted_runs", AsyncMock(return_value=0)),
        patch.object(daemon, "_cleanup_stale_runs", return_value=0),
        patch("robothor.engine.repair_recovery.recover", recover),
        patch("robothor.rooms.recovery.reap_stale_editions", side_effect=RuntimeError("db")),
    ):
        await daemon._recover_interrupted_work(MagicMock(), config)

    recover.assert_awaited_once()
