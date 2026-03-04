#!/usr/bin/env python3
"""
Tests for bridge_watchdog.py — TDD.

Bridge watchdog checks localhost:9100/health every 5 minutes.
On 2 consecutive failures: restarts robothor-bridge.
On success: resets failure counter, auto-resolves stale bridge escalations.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_state(path: Path, consecutive_failures: int):
    path.write_text(
        json.dumps(
            {
                "consecutive_failures": consecutive_failures,
                "last_check": datetime.now().isoformat(),
            }
        )
    )


def _write_handoff(path: Path, escalations: list):
    path.write_text(
        json.dumps(
            {
                "lastRunAt": "2026-02-19T12:00:00+00:00",
                "escalations": escalations,
            }
        )
    )


def _read_handoff(path: Path) -> dict:
    return json.loads(path.read_text())


def _bridge_escalation(resolved: bool = False) -> dict:
    esc = {
        "id": "bridge-fail-20260218",
        "source": "conversation",
        "sourceId": "bridge-health",
        "reason": "CRM Bridge relay failure",
        "summary": "Bridge connection refused on localhost:9100",
        "urgency": "medium",
        "handled": False,
        "createdAt": "2026-02-18T14:33:00+00:00",
        "surfacedAt": "2026-02-18T17:10:00Z",
        "resolvedAt": "2026-02-19T07:15:00Z" if resolved else None,
    }
    return esc


def _email_escalation() -> dict:
    return {
        "id": "email-abc123",
        "source": "email",
        "sourceId": "abc123",
        "reason": "Financial document needs review",
        "summary": "Samantha: Financial draft for review",
        "urgency": "high",
        "createdAt": "2026-02-18T14:00:00+00:00",
        "surfacedAt": "2026-02-18T17:00:00Z",
        "resolvedAt": None,
    }


# ---------------------------------------------------------------------------
# Tests: Health check logic
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """Test the bridge health check function."""

    def test_returns_true_on_200(self):
        from bridge_watchdog import check_bridge_health

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}

        with patch("bridge_watchdog.requests.get", return_value=mock_resp):
            assert check_bridge_health() is True

    def test_returns_false_on_500(self):
        from bridge_watchdog import check_bridge_health

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("bridge_watchdog.requests.get", return_value=mock_resp):
            assert check_bridge_health() is False

    def test_returns_false_on_connection_error(self):
        import requests
        from bridge_watchdog import check_bridge_health

        with patch("bridge_watchdog.requests.get", side_effect=requests.ConnectionError):
            assert check_bridge_health() is False

    def test_returns_false_on_timeout(self):
        import requests
        from bridge_watchdog import check_bridge_health

        with patch("bridge_watchdog.requests.get", side_effect=requests.Timeout):
            assert check_bridge_health() is False


# ---------------------------------------------------------------------------
# Tests: State tracking
# ---------------------------------------------------------------------------


class TestStateTracking:
    """Test consecutive failure counter persistence."""

    def test_loads_existing_state(self, tmp_path):
        from bridge_watchdog import load_state

        state_file = tmp_path / "bridge-watchdog-state.json"
        _write_state(state_file, 3)

        state = load_state(state_file)
        assert state["consecutive_failures"] == 3

    def test_returns_zero_on_missing_file(self, tmp_path):
        from bridge_watchdog import load_state

        state_file = tmp_path / "nonexistent.json"
        state = load_state(state_file)
        assert state["consecutive_failures"] == 0

    def test_returns_zero_on_corrupt_file(self, tmp_path):
        from bridge_watchdog import load_state

        state_file = tmp_path / "corrupt.json"
        state_file.write_text("not json{{{")

        state = load_state(state_file)
        assert state["consecutive_failures"] == 0

    def test_saves_state(self, tmp_path):
        from bridge_watchdog import save_state

        state_file = tmp_path / "bridge-watchdog-state.json"
        save_state(state_file, 2)

        data = json.loads(state_file.read_text())
        assert data["consecutive_failures"] == 2
        assert "last_check" in data


# ---------------------------------------------------------------------------
# Tests: Service restart
# ---------------------------------------------------------------------------


class TestServiceRestart:
    """Test that restart is triggered after consecutive failures."""

    def test_no_restart_on_first_failure(self, tmp_path):
        from bridge_watchdog import run_watchdog

        state_file = tmp_path / "state.json"
        handoff_path = tmp_path / "handoff.json"
        _write_handoff(handoff_path, [])

        with (
            patch("bridge_watchdog.check_bridge_health", return_value=False),
            patch("bridge_watchdog.restart_bridge") as mock_restart,
        ):
            run_watchdog(state_file=state_file, handoff_path=handoff_path)

        mock_restart.assert_not_called()

        state = json.loads(state_file.read_text())
        assert state["consecutive_failures"] == 1

    def test_restart_on_second_consecutive_failure(self, tmp_path):
        from bridge_watchdog import run_watchdog

        state_file = tmp_path / "state.json"
        handoff_path = tmp_path / "handoff.json"
        _write_state(state_file, 1)  # Already failed once
        _write_handoff(handoff_path, [])

        with (
            patch("bridge_watchdog.check_bridge_health", return_value=False),
            patch("bridge_watchdog.restart_bridge") as mock_restart,
        ):
            run_watchdog(state_file=state_file, handoff_path=handoff_path)

        mock_restart.assert_called_once()

        state = json.loads(state_file.read_text())
        assert state["consecutive_failures"] == 2

    def test_restart_on_third_failure_too(self, tmp_path):
        from bridge_watchdog import run_watchdog

        state_file = tmp_path / "state.json"
        handoff_path = tmp_path / "handoff.json"
        _write_state(state_file, 2)
        _write_handoff(handoff_path, [])

        with (
            patch("bridge_watchdog.check_bridge_health", return_value=False),
            patch("bridge_watchdog.restart_bridge") as mock_restart,
        ):
            run_watchdog(state_file=state_file, handoff_path=handoff_path)

        mock_restart.assert_called_once()

    def test_resets_counter_on_success(self, tmp_path):
        from bridge_watchdog import run_watchdog

        state_file = tmp_path / "state.json"
        handoff_path = tmp_path / "handoff.json"
        _write_state(state_file, 3)  # Was failing
        _write_handoff(handoff_path, [])

        with (
            patch("bridge_watchdog.check_bridge_health", return_value=True),
            patch("bridge_watchdog.restart_bridge") as mock_restart,
        ):
            run_watchdog(state_file=state_file, handoff_path=handoff_path)

        mock_restart.assert_not_called()

        state = json.loads(state_file.read_text())
        assert state["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# Tests: Escalation auto-resolve
# ---------------------------------------------------------------------------


class TestEscalationAutoResolve:
    """Test that stale bridge escalations are auto-resolved on healthy check."""

    def test_resolves_bridge_escalation_on_success(self, tmp_path):
        from bridge_watchdog import run_watchdog

        state_file = tmp_path / "state.json"
        handoff_path = tmp_path / "handoff.json"
        _write_handoff(handoff_path, [_bridge_escalation(resolved=False)])

        with patch("bridge_watchdog.check_bridge_health", return_value=True):
            run_watchdog(state_file=state_file, handoff_path=handoff_path)

        data = _read_handoff(handoff_path)
        assert data["escalations"][0]["resolvedAt"] is not None
        assert "watchdog" in data["escalations"][0].get("resolution", "").lower()

    def test_leaves_email_escalation_untouched(self, tmp_path):
        from bridge_watchdog import run_watchdog

        state_file = tmp_path / "state.json"
        handoff_path = tmp_path / "handoff.json"
        _write_handoff(
            handoff_path,
            [
                _bridge_escalation(resolved=False),
                _email_escalation(),
            ],
        )

        with patch("bridge_watchdog.check_bridge_health", return_value=True):
            run_watchdog(state_file=state_file, handoff_path=handoff_path)

        data = _read_handoff(handoff_path)
        # Bridge resolved
        assert data["escalations"][0]["resolvedAt"] is not None
        # Email untouched
        assert data["escalations"][1]["resolvedAt"] is None

    def test_skips_already_resolved_escalation(self, tmp_path):
        from bridge_watchdog import run_watchdog

        state_file = tmp_path / "state.json"
        handoff_path = tmp_path / "handoff.json"
        resolved_esc = _bridge_escalation(resolved=True)
        original_ts = resolved_esc["resolvedAt"]
        _write_handoff(handoff_path, [resolved_esc])

        with patch("bridge_watchdog.check_bridge_health", return_value=True):
            run_watchdog(state_file=state_file, handoff_path=handoff_path)

        data = _read_handoff(handoff_path)
        assert data["escalations"][0]["resolvedAt"] == original_ts

    def test_no_resolve_on_failure(self, tmp_path):
        from bridge_watchdog import run_watchdog

        state_file = tmp_path / "state.json"
        handoff_path = tmp_path / "handoff.json"
        _write_handoff(handoff_path, [_bridge_escalation(resolved=False)])

        with (
            patch("bridge_watchdog.check_bridge_health", return_value=False),
            patch("bridge_watchdog.restart_bridge"),
        ):
            run_watchdog(state_file=state_file, handoff_path=handoff_path)

        data = _read_handoff(handoff_path)
        assert data["escalations"][0]["resolvedAt"] is None

    def test_handles_missing_handoff_file(self, tmp_path):
        from bridge_watchdog import run_watchdog

        state_file = tmp_path / "state.json"
        handoff_path = tmp_path / "nonexistent.json"

        with patch("bridge_watchdog.check_bridge_health", return_value=True):
            # Should not raise
            run_watchdog(state_file=state_file, handoff_path=handoff_path)

    def test_resolves_keyword_match_escalation(self, tmp_path):
        """Escalation without sourceId=bridge-health but with infra keywords."""
        from bridge_watchdog import run_watchdog

        state_file = tmp_path / "state.json"
        handoff_path = tmp_path / "handoff.json"
        keyword_esc = {
            "id": "agent-created-123",
            "source": "conversation",
            "sourceId": "some-other-id",
            "reason": "connection refused on bridge service",
            "summary": "CRM Bridge failing — relay not working",
            "urgency": "medium",
            "createdAt": "2026-02-18T14:33:00+00:00",
            "resolvedAt": None,
        }
        _write_handoff(handoff_path, [keyword_esc])

        with patch("bridge_watchdog.check_bridge_health", return_value=True):
            run_watchdog(state_file=state_file, handoff_path=handoff_path)

        data = _read_handoff(handoff_path)
        assert data["escalations"][0]["resolvedAt"] is not None


# ---------------------------------------------------------------------------
# Tests: Restart function
# ---------------------------------------------------------------------------


class TestRestartBridge:
    """Test the restart_bridge function."""

    def test_calls_systemctl_restart(self):
        from bridge_watchdog import restart_bridge

        with patch("bridge_watchdog.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            restart_bridge()

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "systemctl" in args
        assert "restart" in args
        assert "robothor-bridge" in args

    def test_handles_restart_failure(self):
        from bridge_watchdog import restart_bridge

        with patch("bridge_watchdog.subprocess.run", side_effect=Exception("systemctl failed")):
            # Should not raise
            restart_bridge()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
