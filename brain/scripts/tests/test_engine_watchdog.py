"""Tests for engine_watchdog.py — external engine health check."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import after sys.path setup since brain/scripts isn't a package
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine_watchdog import (
    FAILURE_THRESHOLD,
    check_health,
    load_state,
    run_watchdog,
    save_state,
)


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "engine-watchdog-state.json"


class TestLoadSaveState:
    def test_load_missing_file(self, tmp_path):
        with patch("engine_watchdog.STATE_FILE", tmp_path / "nonexistent.json"):
            state = load_state()
            assert state.get("consecutive_failures", 0) == 0

    def test_save_and_load(self, state_file):
        save_state({"consecutive_failures": 3, "cooldown": 0})


class TestCheckHealth:
    @patch("engine_watchdog.requests.get")
    def test_healthy(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        assert check_health() is True

    @patch("engine_watchdog.requests.get")
    def test_unhealthy_500(self, mock_get):
        mock_get.return_value = MagicMock(status_code=500)
        assert check_health() is False

    @patch("engine_watchdog.requests.get", side_effect=Exception("connection refused"))
    def test_connection_error(self, mock_get):
        assert check_health() is False


class TestRunWatchdog:
    @patch("engine_watchdog.STATE_FILE")
    @patch("engine_watchdog.check_health", return_value=True)
    def test_healthy_resets_counter(self, mock_health, mock_sf, tmp_path):
        sf = tmp_path / "state.json"
        sf.write_text(json.dumps({"consecutive_failures": 1, "cooldown": 0}))
        mock_sf.__str__ = lambda s: str(sf)
        # Patch load/save to use temp file
        with patch("engine_watchdog.load_state", return_value={"consecutive_failures": 1, "cooldown": 0}):
            with patch("engine_watchdog.save_state") as mock_save:
                run_watchdog()
                mock_save.assert_called_once()
                saved = mock_save.call_args[0][0]
                assert saved["consecutive_failures"] == 0

    @patch("engine_watchdog.save_state")
    @patch("engine_watchdog.load_state", return_value={"consecutive_failures": 0, "cooldown": 0})
    @patch("engine_watchdog.check_health", return_value=False)
    def test_failure_increments_counter(self, mock_health, mock_load, mock_save):
        run_watchdog()
        saved = mock_save.call_args[0][0]
        assert saved["consecutive_failures"] == 1

    @patch("engine_watchdog.restart_engine")
    @patch("engine_watchdog.send_telegram_alert")
    @patch("engine_watchdog.save_state")
    @patch("engine_watchdog.load_state", return_value={"consecutive_failures": 1, "cooldown": 0})
    @patch("engine_watchdog.check_health", return_value=False)
    def test_restart_at_threshold(self, mock_health, mock_load, mock_save, mock_alert, mock_restart):
        run_watchdog()
        mock_restart.assert_called_once()
        mock_alert.assert_called_once()
        saved = mock_save.call_args[0][0]
        assert saved["cooldown"] == 2  # COOLDOWN_CYCLES

    @patch("engine_watchdog.send_telegram_alert")
    @patch("engine_watchdog.save_state")
    @patch("engine_watchdog.load_state", return_value={"consecutive_failures": 2, "cooldown": 0})
    @patch("engine_watchdog.check_health", return_value=True)
    def test_recovery_alert(self, mock_health, mock_load, mock_save, mock_alert):
        run_watchdog()
        mock_alert.assert_called_once()
        assert "Recovered" in mock_alert.call_args[0][0]

    @patch("engine_watchdog.requests.post")
    def test_telegram_direct_bot_api(self, mock_post, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("ROBOTHOR_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
        from engine_watchdog import send_telegram_alert
        send_telegram_alert("test message")
        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        assert "api.telegram.org/botfake-token/sendMessage" in url
