"""Fail-closed human-approval when no approver is reachable (Wave-1, PR-11).

init_permission_manager was never called, so get_permission_manager() always
returned None and the runner auto-approved every human_approval escalation. Now
the daemon wires the manager to Telegram, and the no-manager branch is gated by
ROBOTHOR_APPROVAL_* (enforce → deny).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from robothor.engine.permission_escalation import fail_closed_on_missing_manager

_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    for v in (
        "ROBOTHOR_DISABLE_ALL_RIPS",
        "ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED",
        "ROBOTHOR_APPROVAL_MODE",
    ):
        monkeypatch.delenv(v, raising=False)


class TestFailClosedOnMissingManager:
    def test_off_auto_approves(self):
        assert fail_closed_on_missing_manager() is False

    def test_observe_auto_approves(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_APPROVAL_MODE", "observe")
        assert fail_closed_on_missing_manager() is False

    def test_enforce_denies(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_APPROVAL_MODE", "enforce")
        assert fail_closed_on_missing_manager() is True

    def test_enabled_but_no_mode_defaults_observe(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", "1")
        assert fail_closed_on_missing_manager() is False


def test_daemon_wires_permission_manager():
    """The daemon must initialise the escalation manager when a bot exists."""
    daemon_src = (_ROOT / "robothor" / "engine" / "daemon.py").read_text()
    assert "init_permission_manager(" in daemon_src
