"""Tests for robothor.cli.admin — service start/stop, status, tui."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from robothor.cli.admin import cmd_start


class TestCmdStart:
    def test_missing_unit_prints_in_process_alternative(self, monkeypatch, capsys):
        """`robothor start` with no robothor-engine unit installed should point
        at the in-process alternative instead of just saying "skipped"."""

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if cmd[:3] == ["sudo", "systemctl", "start"]:
                result.returncode = 1
                result.stderr = "Failed to start robothor-engine.service: Unit not found."
                result.stdout = ""
            elif cmd[:2] == ["systemctl", "list-unit-files"]:
                result.returncode = 0
                result.stdout = "0 unit files listed.\n"
                result.stderr = ""
            else:
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
            return result

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("robothor.cli.admin.cmd_status", lambda args: 0)

        rc = cmd_start(SimpleNamespace())

        out = capsys.readouterr().out
        assert rc == 0
        assert "robothor engine start" in out
        assert "Unit not found" not in out  # no raw systemctl error text leaked

    def test_no_systemd_available_prints_alternative_without_crashing(self, monkeypatch, capsys):
        """On a box with no systemd/sudo at all (e.g. a plain pip install),
        `robothor start` must not blow up with a raw FileNotFoundError."""

        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("sudo")

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("robothor.cli.admin.cmd_status", lambda args: 0)

        rc = cmd_start(SimpleNamespace())

        out = capsys.readouterr().out
        assert rc == 0
        assert "robothor engine start" in out

    def test_service_without_in_process_alternative_still_skips_cleanly(self, monkeypatch, capsys):
        """robothor-bridge/robothor-voice have no in-process alternative — they
        should just report "skipped", not crash and not fabricate a hint."""

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if cmd[:3] == ["sudo", "systemctl", "start"] and cmd[3] == "robothor-bridge":
                result.returncode = 1
                result.stderr = "Unit not found."
                result.stdout = ""
            elif cmd[:2] == ["systemctl", "list-unit-files"] and "robothor-bridge" in cmd[2]:
                result.returncode = 0
                result.stdout = "0 unit files listed.\n"
                result.stderr = ""
            else:
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
            return result

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("robothor.cli.admin.cmd_status", lambda args: 0)

        rc = cmd_start(SimpleNamespace())

        out = capsys.readouterr().out
        assert rc == 0
        bridge_line = next(line for line in out.splitlines() if "robothor-bridge" in line)
        assert "skipped" in bridge_line
