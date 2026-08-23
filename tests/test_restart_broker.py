"""The agent may ask for a restart. It may not choose what gets restarted.

`robothor-restart.path` watches an agent-writable trigger and
`robothor-restart.service` runs as root. PR #205 built this so the agent could
ask for its own restart without ever holding privilege, and its comment states
the invariant plainly:

    The target unit is HARDCODED. It is never read from the trigger file: that
    file is agent-writable, and letting its contents name a unit would hand an
    injected agent the ability to stop or restart anything on the machine.

The operator works from SSH and is never physically at the box, so the agent
needs the same treatment for a handful of other units — it has been asking him
to run `sudo systemctl restart robothor-delphi-engine.service` by hand. That
list must grow WITHOUT weakening the invariant.

So the request is a FILENAME, matched against a fixed allowlist compiled into a
root-owned handler. File contents are never read, never executed, never used to
name a unit.

Two properties this file exists to defend:

1. A request naming a unit outside the allowlist does nothing. `sshd`, `docker`,
   `postgresql` and `../..` traversal all get refused.
2. The handler is NOT readable-and-writable by the agent. The engine runs with
   ReadWritePaths=/home/philip/robothor, so a handler executed as root from
   inside the repo would let an injected agent rewrite it and gain root —
   exactly the hole #205 closed.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HANDLER = REPO / "infra" / "bin" / "robothor-restart-handler.sh"
UNIT_DIR = REPO / "infra" / "systemd"


def _run(tmp_path: Path, *names: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Drop request files, run the handler with a stubbed systemctl."""
    reqs = tmp_path / "requests"
    reqs.mkdir(exist_ok=True)
    for n in names:
        (reqs / n).write_text("this content must be ignored\nrobothor-engine\n")

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "systemctl.log"
    stub = bindir / "systemctl"
    stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{log}"\nexit 0\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["ROBOTHOR_RESTART_REQUEST_DIR"] = str(reqs)
    proc = subprocess.run(
        ["bash", str(HANDLER)], capture_output=True, text=True, timeout=30, env=env
    )
    return proc, log


def _restarted(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [ln for ln in log.read_text().splitlines() if ln.strip()]


class TestAllowedUnits:
    def test_the_engine_can_still_be_restarted(self, tmp_path: Path):
        """#205's original capability must survive."""
        _, log = _run(tmp_path, "robothor-engine")
        assert any("restart robothor-engine.service" in c for c in _restarted(log))

    def test_the_delphi_engine_can_be_restarted(self, tmp_path: Path):
        """The one the operator has been running by hand over SSH."""
        _, log = _run(tmp_path, "robothor-delphi-engine")
        assert any("restart robothor-delphi-engine.service" in c for c in _restarted(log))

    def test_several_requests_are_all_honoured(self, tmp_path: Path):
        _, log = _run(tmp_path, "robothor-bridge", "robothor-app")
        calls = " ".join(_restarted(log))
        assert "robothor-bridge.service" in calls and "robothor-app.service" in calls

    def test_the_request_is_consumed(self, tmp_path: Path):
        """A leftover trigger would loop the path unit forever."""
        _, log = _run(tmp_path, "robothor-engine")
        assert not list((tmp_path / "requests").iterdir())


class TestRefusedUnits:
    @pytest.mark.parametrize(
        "name", ["sshd", "docker", "postgresql", "tailscaled", "robothor-restart"]
    )
    def test_a_unit_outside_the_allowlist_is_refused(self, tmp_path: Path, name: str):
        _, log = _run(tmp_path, name)
        assert _restarted(log) == [], f"{name} was restarted but is not on the allowlist"

    @pytest.mark.parametrize("name", ["..", "../sshd", "robothor-engine;sshd", "robothor-engine sshd"])
    def test_traversal_and_injection_are_refused(self, tmp_path: Path, name: str):
        try:
            _, log = _run(tmp_path, name)
        except (OSError, ValueError):
            return  # the filesystem refused it first, which is also fine
        assert _restarted(log) == [], f"{name!r} reached systemctl"

    def test_a_refused_request_is_still_consumed(self, tmp_path: Path):
        """Otherwise a bogus name loops the path unit forever."""
        _run(tmp_path, "sshd")
        assert not list((tmp_path / "requests").iterdir())

    def test_file_contents_are_never_used(self, tmp_path: Path):
        """The content says robothor-engine; the NAME is not on the allowlist."""
        _, log = _run(tmp_path, "not-a-real-unit")
        assert _restarted(log) == []


class TestVisionIsNotAgentRestartable:
    def test_vision_is_deliberately_absent(self, tmp_path: Path):
        """Vision was disabled by hand after the 2026-08-19 GPU thermal event.

        Letting the agent re-enable it would let it undo a thermal-safety
        decision unattended. That stays a human action.
        """
        _, log = _run(tmp_path, "robothor-vision")
        assert _restarted(log) == []
        _, log2 = _run(tmp_path, "mediamtx-webcam")
        assert _restarted(log2) == []


class TestTheHandlerIsNotAgentWritable:
    def test_the_unit_does_not_execute_from_the_repo(self):
        """The engine has ReadWritePaths=/home/philip/robothor.

        A root handler executed from inside the repo could be rewritten by an
        injected agent — precisely the escalation #205 closed.
        """
        unit = (UNIT_DIR / "robothor-restart.service").read_text()
        exec_lines = [ln for ln in unit.splitlines() if ln.startswith(("ExecStart", "ExecStartPre"))]
        assert exec_lines, "no ExecStart in the unit"
        for line in exec_lines:
            assert "/home/philip/robothor" not in line, (
                f"root unit executes from the agent-writable repo: {line}"
            )
            assert "/robothor/infra" not in line, f"root unit executes from the repo: {line}"
