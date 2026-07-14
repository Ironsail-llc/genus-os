"""Docker sandbox isolation for computer-use agents.

Provides per-run ephemeral containers for desktop/browser agents.
Two modes:
- LOCAL: existing Xvfb :99 display (backward compatible, default)
- DOCKER: per-run Docker container with Xvfb + Chromium + xdotool
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

# Port range for CDP connections (each sandbox gets a unique port)
_MIN_CDP_PORT = 19222
_MAX_CDP_PORT = 19322
_used_ports: set[int] = set()

SANDBOX_IMAGE = os.environ.get("ROBOTHOR_SANDBOX_IMAGE", "robothor-sandbox:latest")
SANDBOX_STOP_TIMEOUT = 5
SANDBOX_PIDS_LIMIT = 256

# The container's view of the workspace. Kept stable so an agent's commands and
# tool paths mean the same thing inside and out.
SANDBOX_WORKDIR = "/workspace"


def sandbox_binary() -> str:
    """The container runtime to shell out to.

    `docker` needs a daemon whose socket is root-equivalent — handing an agent
    the docker group would defeat the sandbox it enables. Rootless `podman` is
    a drop-in with the same CLI and no daemon, which is why this is a knob and
    not a constant.
    """
    return os.environ.get("ROBOTHOR_SANDBOX_BINARY", "docker")


def sandbox_network() -> str:
    """Container network mode. Default `none` — deny by default.

    Docker's default bridge gives a container full outbound internet *and*
    reaches every host service on the gateway (postgres, the bridge API on
    172.17.0.1). For a boundary meant to contain a prompt-injected agent that
    is the wrong default. Browser agents need connectivity and must opt in
    explicitly via ROBOTHOR_SANDBOX_NETWORK=bridge.
    """
    return os.environ.get("ROBOTHOR_SANDBOX_NETWORK", "none")


class SandboxMode(StrEnum):
    LOCAL = "local"
    DOCKER = "docker"


@dataclass
class Sandbox:
    """Execution sandbox for desktop/browser tools."""

    mode: SandboxMode = SandboxMode.LOCAL
    container_id: str | None = None
    display: str = ":99"
    cdp_port: int | None = None
    run_id: str = ""
    workspace: str = ""
    _started: bool = False

    def _run_argv(self, workspace: str, cdp_port: int | None = None) -> list[str]:
        """Build the `run` argv.

        Extracted from start() so the isolation flags are testable without a
        container runtime — they are the whole security value of this class and
        were previously absent: no mount, no --network, no --user, no --cap-drop.
        """
        binary = sandbox_binary()
        argv = [
            binary,
            "run",
            "-d",
            "--name",
            f"sandbox-{self.run_id[:12]}",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "--tmpfs",
            "/run:rw,noexec,nosuid,size=64m",
            # The workspace. Without this the container cannot see the repo and
            # every `exec` lands in an empty filesystem.
            "-v",
            f"{workspace}:{SANDBOX_WORKDIR}:rw",
            "--workdir",
            SANDBOX_WORKDIR,
            # Deny-by-default networking (see sandbox_network).
            "--network",
            sandbox_network(),
            # Containment.
            "--cap-drop=ALL",
            "--security-opt",
            "no-new-privileges",
            f"--pids-limit={SANDBOX_PIDS_LIMIT}",
            "--memory",
            "512m",
            "--cpus",
            "1.0",
            "--stop-timeout",
            str(SANDBOX_STOP_TIMEOUT),
        ]

        # Run as the engine user, not root-in-container. Under rootless podman
        # keep-id maps the host uid to the same uid inside, so files written to
        # the mounted workspace stay owned by the engine user rather than
        # arriving as root-owned and unreadable on the host.
        if binary == "podman":
            argv += ["--userns=keep-id"]
        argv += ["--user", f"{os.getuid()}:{os.getgid()}"]

        if cdp_port is not None:
            argv += ["-p", f"{cdp_port}:9222"]

        argv.append(SANDBOX_IMAGE)
        return argv

    def _container_argv(self, command: str) -> list[str]:
        """argv to run a shell command inside the container.

        The exec tool hands us a shell *string* — pipes, redirects, heredocs.
        Splitting it would break every real command the agents run, so it goes
        to `sh -c` verbatim.
        """
        return [
            sandbox_binary(),
            "exec",
            "--workdir",
            SANDBOX_WORKDIR,
            str(self.container_id),
            "sh",
            "-c",
            command,
        ]

    async def exec_shell(self, command: str, timeout: int = 30) -> dict[str, Any]:
        """Run a shell command inside the sandbox.

        Raises on infrastructure failure so the caller fails *closed* — a
        broken container must never silently fall back to the host (#201).
        """
        if not self.container_id:
            raise RuntimeError("sandbox has no container")

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                self._container_argv(command),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"error": f"Command timed out ({timeout}s limit)", "exit_code": 124}

        return {
            "stdout": proc.stdout[:4000],
            "stderr": proc.stderr[:2000],
            "exit_code": proc.returncode,
        }

    async def start(self) -> None:
        """Start the sandbox (container or no-op for local)."""
        if self.mode == SandboxMode.LOCAL:
            self._started = True
            return

        cdp_port = self._allocate_port()
        self.cdp_port = cdp_port

        cmd = self._run_argv(workspace=self.workspace, cdp_port=cdp_port)

        try:
            proc = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                ),
            )
            if proc.returncode != 0:
                logger.error("Failed to start sandbox: %s", proc.stderr)
                raise RuntimeError(f"Sandbox start failed: {proc.stderr}")

            self.container_id = proc.stdout.strip()[:64]
            self.display = ":0"  # Inside container
            self._started = True

            # Wait for Xvfb to be ready
            await asyncio.sleep(1)
            logger.info("Sandbox started: %s (CDP port %d)", self.container_id[:12], cdp_port)

        except subprocess.TimeoutExpired as e:
            raise RuntimeError("Sandbox start timed out") from e

    async def exec(self, cmd: list[str], timeout: int = 30) -> dict[str, Any]:
        """Execute a command inside the sandbox.

        For LOCAL mode, runs via subprocess with DISPLAY set.
        For DOCKER mode, runs via docker exec.
        """
        if self.mode == SandboxMode.LOCAL:
            env = os.environ.copy()
            env["DISPLAY"] = self.display
            try:
                proc = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        env=env,
                    ),
                )
                if proc.returncode != 0:
                    return {"error": proc.stderr.strip(), "exit_code": proc.returncode}
                return {"stdout": proc.stdout.strip(), "exit_code": 0}
            except subprocess.TimeoutExpired:
                return {"error": f"Command timed out ({timeout}s)"}

        if not self.container_id:
            return {"error": "Sandbox not started"}

        docker_cmd = [sandbox_binary(), "exec", str(self.container_id)] + cmd
        try:
            proc = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    docker_cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                ),
            )
            if proc.returncode != 0:
                return {"error": proc.stderr.strip(), "exit_code": proc.returncode}
            return {"stdout": proc.stdout.strip(), "exit_code": 0}
        except subprocess.TimeoutExpired:
            return {"error": f"Docker exec timed out ({timeout}s)"}

    async def copy_from(self, container_path: str, local_path: str) -> bool:
        """Copy a file from the sandbox to the host. No-op for local mode."""
        if self.mode == SandboxMode.LOCAL:
            return True  # File is already local
        if not self.container_id:
            return False

        cmd = [sandbox_binary(), "cp", f"{self.container_id}:{container_path}", local_path]
        try:
            proc = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=10),
            )
            return proc.returncode == 0
        except Exception as e:
            logger.error("docker cp failed: %s", e)
            return False

    def browser_endpoint(self) -> str:
        """Get the CDP WebSocket endpoint for browser connection."""
        if self.mode == SandboxMode.LOCAL:
            return ""  # Use local Playwright launch
        return f"http://localhost:{self.cdp_port}"

    async def stop(self) -> None:
        """Stop and remove the sandbox container."""
        if self.mode == SandboxMode.LOCAL or not self.container_id:
            return

        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    [sandbox_binary(), "rm", "-f", str(self.container_id)],
                    capture_output=True,
                    timeout=15,
                ),
            )
            logger.info("Sandbox stopped: %s", self.container_id[:12])
        except Exception as e:
            logger.warning("Failed to stop sandbox %s: %s", self.container_id[:12], e)
        finally:
            if self.cdp_port is not None:
                _used_ports.discard(self.cdp_port)
            self.container_id = None
            self._started = False

    def _allocate_port(self) -> int:
        """Allocate a unique CDP port."""
        for port in range(_MIN_CDP_PORT, _MAX_CDP_PORT):
            if port not in _used_ports:
                _used_ports.add(port)
                return port
        raise RuntimeError("No available CDP ports")


# ─── ContextVar for per-run sandbox ──────────────────────────────────

_current_sandbox: ContextVar[Sandbox | None] = ContextVar("_current_sandbox", default=None)

# Public alias — tests and handlers set/reset the sandbox around a run.
sandbox_var = _current_sandbox


def get_current_sandbox() -> Sandbox | None:
    """Get the sandbox for the current agent run (if any)."""
    return _current_sandbox.get()


def set_current_sandbox(sandbox: Sandbox | None) -> None:
    """Set the sandbox for the current agent run."""
    _current_sandbox.set(sandbox)
