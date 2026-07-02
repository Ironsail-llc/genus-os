"""Desktop automation routes through a Docker sandbox when present (PR-9).

With a per-run Docker sandbox active, xdotool runs inside the container
(docker exec) so a sandboxed agent can't drive the operator's real screen.
With no sandbox (default), it's the unchanged host path.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import robothor.engine.sandbox as sandbox_mod
from robothor.engine.tools.handlers import desktop


class _FakeProc:
    returncode = 0
    stdout = "ok"
    stderr = ""


def test_host_path_unchanged_without_sandbox(monkeypatch):
    monkeypatch.setattr(sandbox_mod, "get_current_sandbox", lambda: None)
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    desktop._run_xdotool("mousemove", "10", "20")
    assert captured["cmd"][0] == "xdotool"  # host path, no docker wrapper


def test_routes_through_container_with_docker_sandbox(monkeypatch):
    fake_sandbox = SimpleNamespace(container_id="deadbeefcafe")
    monkeypatch.setattr(sandbox_mod, "get_current_sandbox", lambda: fake_sandbox)
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    desktop._run_xdotool("click", "1")
    assert captured["cmd"][:3] == ["docker", "exec", "-e"]
    assert "deadbeefcafe" in captured["cmd"]
    assert "xdotool" in captured["cmd"]
