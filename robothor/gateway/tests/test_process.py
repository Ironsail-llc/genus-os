"""Tests for GatewayProcess."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from robothor.gateway.process import GatewayProcess


class TestStart:
    def test_raises_when_not_built(self, unbuilt_gateway_dir: Path):
        proc = GatewayProcess(
            gateway_dir=unbuilt_gateway_dir, pidfile=unbuilt_gateway_dir / "gw.pid"
        )
        with pytest.raises(FileNotFoundError, match="not built"):
            proc.start()

    @patch("subprocess.Popen")
    def test_starts_process_and_writes_pid(
        self, mock_popen, tmp_gateway_dir: Path, tmp_path: Path
    ):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        pidfile = tmp_path / "gw.pid"
        gw = GatewayProcess(
            gateway_dir=tmp_gateway_dir,
            config_dir=tmp_path / "config",
            pidfile=pidfile,
        )
        pid = gw.start()
        assert pid == 12345
        assert pidfile.read_text().strip() == "12345"


class TestStop:
    def test_returns_false_when_no_pidfile(self, tmp_path: Path):
        gw = GatewayProcess(pidfile=tmp_path / "nonexistent.pid")
        assert gw.stop() is False

    @patch("os.kill")
    def test_stops_process_by_pid(self, mock_kill, tmp_path: Path):
        pidfile = tmp_path / "gw.pid"
        pidfile.write_text("99999")
        # First kill(SIGTERM) succeeds, second kill(0) raises = process gone
        mock_kill.side_effect = [None, OSError("No such process")]

        gw = GatewayProcess(pidfile=pidfile)
        assert gw.stop() is True
        assert not pidfile.exists()


class TestIsRunning:
    def test_false_when_no_pidfile(self, tmp_path: Path):
        gw = GatewayProcess(pidfile=tmp_path / "nonexistent.pid")
        assert gw.is_running() is False

    @patch("os.kill")
    def test_true_when_process_alive(self, mock_kill, tmp_path: Path):
        pidfile = tmp_path / "gw.pid"
        pidfile.write_text("12345")
        mock_kill.return_value = None

        gw = GatewayProcess(pidfile=pidfile)
        assert gw.is_running() is True

    @patch("os.kill", side_effect=OSError)
    def test_false_when_process_dead(self, mock_kill, tmp_path: Path):
        pidfile = tmp_path / "gw.pid"
        pidfile.write_text("12345")

        gw = GatewayProcess(pidfile=pidfile)
        assert gw.is_running() is False


class TestHealthCheck:
    @patch("httpx.get")
    def test_healthy_response(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        gw = GatewayProcess()
        result = gw.health_check()
        assert result["healthy"] is True

    @patch("httpx.get", side_effect=Exception("Connection refused"))
    def test_unhealthy_response(self, mock_get):
        gw = GatewayProcess()
        result = gw.health_check()
        assert result["healthy"] is False
        assert "Connection refused" in result["error"]


class TestGenerateSystemdUnit:
    def test_generates_valid_unit(self, tmp_gateway_dir: Path):
        gw = GatewayProcess(gateway_dir=tmp_gateway_dir)
        unit = gw.generate_systemd_unit()
        assert "[Unit]" in unit
        assert "[Service]" in unit
        assert "[Install]" in unit
        assert "Restart=always" in unit
        assert "KillMode=control-group" in unit
        assert str(tmp_gateway_dir / "dist" / "index.js") in unit
