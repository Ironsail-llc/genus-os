"""Desktop automation routes through a sandbox container when present (PR-9).

With a per-run sandbox active, xdotool runs inside the container so a sandboxed
agent can't drive the operator's real screen. With no sandbox (default), it's
the unchanged host path.

The wrapper runtime comes from ``sandbox_binary()``. It used to be a hardcoded
``docker``, which contradicted ``sandbox.py``'s deliberate preference for
rootless podman and could not have run on this box at all (the engine user is
not in the docker group), so the assertions here name the accessor rather than
a literal.
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


def test_routes_through_container_with_sandbox(monkeypatch):
    from robothor.engine.sandbox import sandbox_binary

    fake_sandbox = SimpleNamespace(container_id="deadbeefcafe")
    monkeypatch.setattr(sandbox_mod, "get_current_sandbox", lambda: fake_sandbox)
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    desktop._run_xdotool("click", "1")
    assert captured["cmd"][:3] == [sandbox_binary(), "exec", "-e"]
    assert "deadbeefcafe" in captured["cmd"]
    assert "xdotool" in captured["cmd"]


def test_explicit_container_without_runtime_never_drives_host(monkeypatch):
    import pytest

    monkeypatch.setattr(
        sandbox_mod,
        "get_current_sandbox",
        lambda: SimpleNamespace(container_id=None, mode=sandbox_mod.SandboxMode.DOCKER),
    )
    with pytest.raises(RuntimeError, match="host fallback is disabled"):
        desktop._desktop_command(["wmctrl", "-lG"])


def test_all_desktop_commands_use_selected_container(monkeypatch):
    monkeypatch.setattr(
        sandbox_mod, "get_current_sandbox", lambda: SimpleNamespace(container_id="test")
    )
    for command in (["wmctrl", "-lG"], ["wmctrl", "-ia", "5"], ["scrot", "-o", "/tmp/test.png"]):
        wrapped = desktop._desktop_command(command)
        assert wrapped[-len(command) :] == command
        assert wrapped[:2] == [sandbox_mod.sandbox_binary(), "exec"]
