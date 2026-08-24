"""A manifest that stops parsing must reach the operator, not just the log.

On 2026-08-23 a YAML indentation error in docs/agents/main.yaml removed the
primary agent from the fleet. The engine stayed `active (running)`, so
`OnFailure=` never fired. The error was logged 109 times over 3h48m and reached
nobody. Four independent controls that should have caught it were inert.

These tests pin the two properties that keep this one from joining them: it
pages at a level that actually interrupts, and it does not page so often that
it gets muted.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.config import ManifestFailure, ManifestScan
from robothor.engine.manifest_guard import alert_manifest_scan


@pytest.fixture
def _state(tmp_path, monkeypatch):
    path = tmp_path / "manifest-guard-alerts.json"
    monkeypatch.setenv("ROBOTHOR_MANIFEST_GUARD_STATE", str(path))
    monkeypatch.setenv("ROBOTHOR_MANIFEST_ALERT_DEDUP", "3600")
    return path


def _dirty(*filenames: str) -> ManifestScan:
    return ManifestScan(
        manifests=(),
        failures=tuple(
            ManifestFailure(f, "YAMLError", "expected <block end>, but found '-'")
            for f in filenames
        ),
        scanned=len(filenames),
        dir_readable=True,
    )


@pytest.mark.asyncio
async def test_dirty_scan_pages_critical(_state):
    """The level is the assertion.

    "warning" routes to a crm_agent_notifications row addressed to_agent=main,
    read by main's heartbeat — and in this incident main's heartbeat had just
    been deleted and main's config would not load. A warning files the alarm in
    the corpse's inbox. Every manifest-parse alert is structurally liable to be
    about the agent that reads warnings, so this one must page.

    If someone later softens this to "warning", this test must break.
    """
    mock_alert = AsyncMock(return_value=True)
    with patch("robothor.engine.manifest_guard.alert", mock_alert):
        await alert_manifest_scan(_dirty("main.yaml"), context="watchdog reconcile")

    mock_alert.assert_awaited_once()
    assert mock_alert.await_args[0][0] == "critical"
    body = mock_alert.await_args[0][2]
    assert "main.yaml" in body
    assert "YAMLError" in body


@pytest.mark.asyncio
async def test_the_page_says_nothing_was_pruned(_state):
    """The operator's first question. Answer it in the message."""
    mock_alert = AsyncMock(return_value=True)
    with patch("robothor.engine.manifest_guard.alert", mock_alert):
        await alert_manifest_scan(_dirty("main.yaml"), context="watchdog reconcile")

    assert "no schedules were pruned" in mock_alert.await_args[0][2].lower()


@pytest.mark.asyncio
async def test_a_clean_scan_never_pages(_state):
    mock_alert = AsyncMock(return_value=True)
    with patch("robothor.engine.manifest_guard.alert", mock_alert):
        await alert_manifest_scan(ManifestScan(scanned=3), context="scheduler start")

    mock_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeat_failure_within_the_floor_does_not_page(_state):
    """Reconcile runs every 5 minutes. Without a floor a single broken file
    pages 12 times an hour and the pager gets muted within a week."""
    mock_alert = AsyncMock(return_value=True)
    with patch("robothor.engine.manifest_guard.alert", mock_alert):
        await alert_manifest_scan(_dirty("main.yaml"), context="reconcile")
        await alert_manifest_scan(_dirty("main.yaml"), context="reconcile")
        await alert_manifest_scan(_dirty("main.yaml"), context="reconcile")

    assert mock_alert.await_count == 1


@pytest.mark.asyncio
async def test_a_second_broken_file_pages_immediately(_state):
    """The dedup key is the SET of failing files, so a new file breaking is an
    escalation, not a repeat — it must not be swallowed by the floor."""
    mock_alert = AsyncMock(return_value=True)
    with patch("robothor.engine.manifest_guard.alert", mock_alert):
        await alert_manifest_scan(_dirty("main.yaml"), context="reconcile")
        await alert_manifest_scan(_dirty("main.yaml", "curator.yaml"), context="reconcile")

    assert mock_alert.await_count == 2
    assert "curator.yaml" in mock_alert.await_args[0][2]


@pytest.mark.asyncio
async def test_the_floor_expires(_state):
    mock_alert = AsyncMock(return_value=True)
    with patch("robothor.engine.manifest_guard.alert", mock_alert):
        await alert_manifest_scan(_dirty("main.yaml"), context="reconcile")
        # age the stamp past the floor
        state = json.loads(_state.read_text())
        state = {k: time.time() - 7200 for k in state}
        _state.write_text(json.dumps(state))
        await alert_manifest_scan(_dirty("main.yaml"), context="reconcile")

    assert mock_alert.await_count == 2


@pytest.mark.asyncio
async def test_recovery_sends_info_not_critical(_state):
    """Learning that it noticed the fix — without being interrupted — is what
    makes an operator trust the control."""
    mock_alert = AsyncMock(return_value=True)
    with patch("robothor.engine.manifest_guard.alert", mock_alert):
        await alert_manifest_scan(_dirty("main.yaml"), context="reconcile")
        await alert_manifest_scan(ManifestScan(scanned=3), context="reconcile")

    assert mock_alert.await_count == 2
    assert mock_alert.await_args[0][0] == "info"
    assert not json.loads(_state.read_text()), "the cleared key must not linger"


@pytest.mark.asyncio
async def test_an_unreadable_dir_pages(_state):
    mock_alert = AsyncMock(return_value=True)
    with patch("robothor.engine.manifest_guard.alert", mock_alert):
        await alert_manifest_scan(ManifestScan(dir_readable=False), context="reconcile")

    mock_alert.assert_awaited_once()
    assert mock_alert.await_args[0][0] == "critical"
    assert "unreadable" in mock_alert.await_args[0][2].lower()


@pytest.mark.asyncio
async def test_alert_failure_never_raises(_state):
    """An alerting failure must never stop reconciliation."""
    with patch(
        "robothor.engine.manifest_guard.alert", AsyncMock(side_effect=RuntimeError("telegram down"))
    ):
        await alert_manifest_scan(_dirty("main.yaml"), context="reconcile")


@pytest.mark.asyncio
async def test_an_undelivered_page_is_not_stamped(_state):
    """Checked, not assumed. If the page did not land, the next tick retries —
    the same discipline as alerts.py (`delivered = bool(sent)`), where assuming
    success hid an arity bug while 432+ alerts went nowhere."""
    with patch("robothor.engine.manifest_guard.alert", AsyncMock(return_value=False)):
        await alert_manifest_scan(_dirty("main.yaml"), context="reconcile")

    assert not _state.exists() or not json.loads(_state.read_text())
