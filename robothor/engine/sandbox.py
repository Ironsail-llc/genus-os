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
    _started: bool = False

    async def start(self) -> None:
        """Start the sandbox (Docker container or no-op for local)."""
        if self.mode == SandboxMode.LOCAL:
            self._started = True
            return

        cdp_port = self._allocate_port()
        self.cdp_port = cdp_port

        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            f"sandbox-{self.run_id[:12]}",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "--tmpfs",
            "/run:rw,noexec,nosuid,size=64m",
            "-p",
            f"{cdp_port}:9222",
            "--stop-timeout",
            str(SANDBOX_STOP_TIMEOUT),
            "--memory",
            "512m",
            "--cpus",
            "1.0",
            SANDBOX_IMAGE,
        ]

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
                raise RuntimeError(f"Docker sandbox start failed: {proc.stderr}")

            self.container_id = proc.stdout.strip()[:64]
            self.display = ":0"  # Inside container
            self._started = True

            # Wait for Xvfb to be ready
            await asyncio.sleep(1)
            logger.info("Sandbox started: %s (CDP port %d)", self.container_id[:12], cdp_port)

        except subprocess.TimeoutExpired as e:
            raise RuntimeError("Docker sandbox start timed out") from e

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

        docker_cmd = ["docker", "exec", self.container_id] + cmd
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

        cmd = ["docker", "cp", f"{self.container_id}:{container_path}", local_path]
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
                    ["docker", "rm", "-f", str(self.container_id)],
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


def get_current_sandbox() -> Sandbox | None:
    """Get the sandbox for the current agent run (if any)."""
    return _current_sandbox.get()


def set_current_sandbox(sandbox: Sandbox | None) -> None:
    """Set the sandbox for the current agent run."""
    _current_sandbox.set(sandbox)
