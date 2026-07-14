"""The `exec` tool must actually run inside the sandbox.

Today it does not. `tools/handlers/filesystem.py` calls `subprocess.run(command,
shell=True, cwd=ctx.workspace)` and never consults `get_current_sandbox()` —
so an agent configured `sandbox: docker` still runs every shell command on the
host, as the engine user. Only `browser.py` and `desktop.py` route into the
container, and none of the sandboxed agents use those tools.

That makes the sandbox decoration: the flag says "contained", the command runs
on the host. These tests pin the routing, plus the two things that have to be
true for the routing to be *usable* and *safe*:

  * usable  — the container must mount the workspace, or every `exec` lands in
              an empty filesystem and the sandbox is worse than useless;
  * safe    — `docker run` today passes no `--network`, no `--cap-drop`, no
              `--user`, no `--pids-limit`. A "sandbox" with full outbound
              internet, host services on 172.17.0.1, and root in-container is
              not a boundary.

And the runtime is hardcoded to `"docker"` in four places, which blocks rootless
podman — the thing that removes the root-equivalent docker socket entirely.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest  # noqa: TC002

from robothor.engine.sandbox import Sandbox, SandboxMode, sandbox_binary, sandbox_var


class _FakeCtx:
    """Minimal ToolContext stand-in."""

    def __init__(self, workspace: str = "/srv/agent-workspace") -> None:
        self.workspace = workspace
        self.agent_id = "auto-agent"


def _exec_handler():
    from robothor.engine.tools.handlers.filesystem import HANDLERS

    return HANDLERS["exec"]


class TestExecRoutesThroughTheSandbox:
    async def test_exec_runs_in_the_container_when_a_sandbox_is_active(self) -> None:
        """The whole point: a docker-mode sandbox must capture `exec`."""
        calls: list[list[str]] = []

        class _Recording(Sandbox):
            async def exec_shell(self, command: str, timeout: int = 30) -> dict[str, Any]:
                calls.append([command])
                return {"stdout": "in-container", "exit_code": 0}

        sb = _Recording(mode=SandboxMode.DOCKER, container_id="deadbeef", run_id="r1")
        token = sandbox_var.set(sb)
        try:
            result = await _exec_handler()({"command": "whoami"}, _FakeCtx())
        finally:
            sandbox_var.reset(token)

        assert calls == [["whoami"]], (
            "exec must route to the active sandbox — today it calls subprocess.run "
            "on the host unconditionally and the sandbox flag is decoration"
        )
        assert result["stdout"] == "in-container"

    async def test_exec_runs_on_the_host_when_no_sandbox_is_active(self) -> None:
        """Local/host mode must keep working exactly as before."""
        result = await _exec_handler()(
            {"command": "echo hello"}, _FakeCtx(workspace=str(Path.cwd()))
        )
        assert result["exit_code"] == 0
        assert "hello" in result["stdout"]

    async def test_a_local_mode_sandbox_still_runs_on_the_host(self) -> None:
        sb = Sandbox(mode=SandboxMode.LOCAL, run_id="r2")
        token = sandbox_var.set(sb)
        try:
            result = await _exec_handler()({"command": "echo local"}, _FakeCtx(str(Path.cwd())))
        finally:
            sandbox_var.reset(token)
        assert result["exit_code"] == 0
        assert "local" in result["stdout"]

    async def test_container_exec_failure_is_reported_not_silently_run_on_host(self) -> None:
        """Fail closed. A broken container must not fall back to the host."""

        class _Broken(Sandbox):
            async def exec_shell(self, command: str, timeout: int = 30) -> dict[str, Any]:
                raise RuntimeError("container is gone")

        sb = _Broken(mode=SandboxMode.DOCKER, container_id="dead", run_id="r3")
        token = sandbox_var.set(sb)
        try:
            result = await _exec_handler()({"command": "rm -rf /"}, _FakeCtx())
        finally:
            sandbox_var.reset(token)

        assert "error" in result, "a failing sandbox must surface an error"
        assert "container is gone" in str(result["error"])


class TestExecShellPreservesShellSemantics:
    """Agents' real commands are shell strings — pipes, redirects, heredocs."""

    def test_exec_shell_wraps_the_command_in_a_shell(self) -> None:
        sb = Sandbox(mode=SandboxMode.DOCKER, container_id="abc", run_id="r4")
        argv = sb._container_argv("ls | wc -l")
        assert argv[:2] == [sandbox_binary(), "exec"]
        assert argv[-3:] == ["sh", "-c", "ls | wc -l"], (
            "the exec tool passes a shell string; splitting it would break every "
            "pipe, redirect and heredoc the agents actually use"
        )


class TestTheContainerIsUsable:
    def test_docker_run_mounts_the_workspace(self) -> None:
        sb = Sandbox(mode=SandboxMode.DOCKER, run_id="r5")
        cmd = sb._run_argv(workspace="/srv/agent-workspace")
        joined = " ".join(cmd)
        assert "-v" in cmd, "no volume mount at all — the container cannot see the repo"
        assert "/srv/agent-workspace" in joined
        assert "--workdir" in cmd, "exec must land in the workspace, not /"


class TestTheContainerIsABoundary:
    def test_capabilities_are_dropped(self) -> None:
        cmd = Sandbox(mode=SandboxMode.DOCKER, run_id="r6")._run_argv(workspace="/w")
        assert "--cap-drop=ALL" in cmd

    def test_no_new_privileges_inside_the_container(self) -> None:
        cmd = Sandbox(mode=SandboxMode.DOCKER, run_id="r7")._run_argv(workspace="/w")
        assert "--security-opt" in cmd
        assert "no-new-privileges" in " ".join(cmd)

    def test_pids_are_limited(self) -> None:
        cmd = Sandbox(mode=SandboxMode.DOCKER, run_id="r8")._run_argv(workspace="/w")
        assert any(c.startswith("--pids-limit") for c in cmd), "fork bomb takes the host down"

    def test_the_network_is_restricted(self) -> None:
        """Default bridge = full outbound internet + host services on 172.17.0.1."""
        cmd = Sandbox(mode=SandboxMode.DOCKER, run_id="r9")._run_argv(workspace="/w")
        assert "--network" in cmd, (
            "no --network flag: the sandbox can reach the internet and every host "
            "service (postgres, the bridge) on the docker bridge gateway"
        )

    def test_it_does_not_run_as_root_in_the_container(self) -> None:
        cmd = Sandbox(mode=SandboxMode.DOCKER, run_id="r10")._run_argv(workspace="/w")
        assert "--user" in cmd


class TestRuntimeIsNotHardcoded:
    """Rootless podman is what removes the root-equivalent docker socket."""

    def test_binary_defaults_to_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ROBOTHOR_SANDBOX_BINARY", raising=False)
        assert sandbox_binary() == "docker"

    def test_binary_is_overridable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROBOTHOR_SANDBOX_BINARY", "podman")
        assert sandbox_binary() == "podman"
        cmd = Sandbox(mode=SandboxMode.DOCKER, run_id="r11")._run_argv(workspace="/w")
        assert cmd[0] == "podman", "the runtime is hardcoded to 'docker' in four places"
