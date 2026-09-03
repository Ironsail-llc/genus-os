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
import shutil
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


#: Extra attempts `Sandbox.start()` makes after the first one fails. 1 covers
#: the observed boot race without turning a real misconfiguration into a loop.
DEFAULT_START_RETRIES = 1

#: Seconds between those attempts. Long enough for `newuidmap` and the
#: subuid/subgid plumbing to finish coming up, short enough that a run waiting
#: on a genuinely dead runtime finds out promptly.
DEFAULT_START_RETRY_SECONDS = 3.0


def _env_number(name: str, default: float) -> float:
    """Read an operator override, falling back rather than failing closed."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s is not a number (%r) — using %s", name, raw, default)
        return default


def start_retries() -> int:
    """How many times to retry a failed container start. 0 disables retrying."""
    return max(0, int(_env_number("ROBOTHOR_SANDBOX_START_RETRIES", DEFAULT_START_RETRIES)))


def start_retry_seconds() -> float:
    """How long to wait before the retry."""
    return max(
        0.0, _env_number("ROBOTHOR_SANDBOX_START_RETRY_SECONDS", DEFAULT_START_RETRY_SECONDS)
    )


def sandbox_binary() -> str:
    """The container runtime to shell out to, safest available first.

    `docker` needs a daemon whose socket is root-equivalent — handing an agent
    the docker group would defeat the sandbox it enables. Rootless `podman` is
    a drop-in with the same CLI and no daemon.

    This used to default to `docker` regardless, which contradicted the
    paragraph above AND did not work: on this instance the engine user is not
    in the docker group, so `docker ps` is permission-denied while rootless
    podman runs fine. An agent declaring `sandbox: docker` could not have
    started a container at all. Nothing broke only because every manifest
    says `sandbox: host` — which is to say the sandbox had never run here.

    A security default that cannot execute is not a security default. An
    explicit `ROBOTHOR_SANDBOX_BINARY` always wins; otherwise prefer the
    rootless runtime that is actually installed.
    """
    explicit = os.environ.get("ROBOTHOR_SANDBOX_BINARY", "").strip()
    if explicit:
        return explicit
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    # Neither installed. Name the safer one so the caller's failure message
    # points at what should be installed, rather than at a daemon we would
    # not want anyway.
    return "podman"


def sandbox_network() -> str:
    """Container network mode. Default `none` — deny by default.

    Docker's default bridge gives a container full outbound internet *and*
    reaches every host service on the gateway (postgres, the bridge API on
    172.17.0.1). For a boundary meant to contain a prompt-injected agent that
    is the wrong default. Browser agents need connectivity and must opt in
    explicitly via ROBOTHOR_SANDBOX_NETWORK=bridge.
    """
    return os.environ.get("ROBOTHOR_SANDBOX_NETWORK", "none")


#: Env var naming the sandbox backend to use. Empty (the default) means the
#: built-in container runtime.
SANDBOX_BACKEND_ENV = "ROBOTHOR_SANDBOX_BACKEND"


def active_sandbox_backend() -> Any:
    """The plugin backend the operator selected, or None for the built-in.

    Every other plugin seam in Genus takes effect the moment a package is
    installed. That is right for a tool or a model and wrong here: this
    class is what confines untrusted execution, and a package able to
    replace it by merely being present could replace it with a no-op while
    nothing looked different. So installation is not selection — a backend
    is inert until the operator names it in ``ROBOTHOR_SANDBOX_BACKEND``.

    Naming one that is not installed raises rather than falling back. A
    silent fall-back to the built-in runtime would turn a misconfigured
    hardening step into an invisible downgrade, which is the failure this
    whole seam exists to avoid.
    """
    name = os.environ.get(SANDBOX_BACKEND_ENV, "").strip()
    if not name:
        return None

    try:
        from robothor.plugins import load_plugins

        loaded = load_plugins(reserved_names=set())
        backends = loaded.sandboxes or {}
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"sandbox backend {name!r} requested but plugins failed to load: {exc}"
        ) from exc

    spec = backends.get(name)
    if spec is None:
        raise RuntimeError(
            f"sandbox backend {name!r} is not installed "
            f"(available: {sorted(backends) or 'none'}). Refusing to fall back "
            "to the built-in runtime silently."
        )
    if not isinstance(spec, dict) or not callable(spec.get("build_argv")):
        raise RuntimeError(f"sandbox backend {name!r} does not provide a callable build_argv")
    return spec


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

    @property
    def container_name(self) -> str:
        """The `--name` this sandbox claims. One definition, because the retry
        has to be able to release exactly the name the run argv takes."""
        return f"sandbox-{self.run_id[:12]}"

    def _run_argv(self, workspace: str, cdp_port: int | None = None) -> list[str]:
        """Build the `run` argv.

        Extracted from start() so the isolation flags are testable without a
        container runtime — they are the whole security value of this class and
        were previously absent: no mount, no --network, no --user, no --cap-drop.
        """
        backend = active_sandbox_backend()
        if backend is not None:
            # The operator selected a plugin runtime. It owns the whole argv:
            # a backend that could only prepend flags could not express a
            # different isolation model, which is the point of having one.
            return list(
                backend["build_argv"](workspace=workspace, run_id=self.run_id, cdp_port=cdp_port)
            )

        binary = sandbox_binary()
        argv = [
            binary,
            "run",
            "-d",
            "--name",
            self.container_name,
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

        # A port mapping is meaningless without a network, and passing both makes
        # podman emit "Port mappings have been discarded as one of the Host,
        # Container, Pod, and None network modes are in use" — which lands in the
        # engine's error path and reads like the sandbox failed to start.
        # Only the browser (CDP) needs the port, and it needs a network anyway.
        if cdp_port is not None and sandbox_network() != "none":
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
        """Start the sandbox (container or no-op for local).

        One bounded retry on a non-zero exit. Rootless podman needs the setuid
        `newuidmap` helper and the invoking user's subuid/subgid ranges, and
        early in boot that plumbing is not always ready:

            newuidmap: write to uid_map failed: Operation not permitted

        Seen once, twelve minutes after a reboot on 2026-09-03, and never
        since — a boot race, which is exactly the failure a single attempt
        turns into a dead run for an agent that declared `sandbox: docker`.

        A retry, not a loop. A runtime that is genuinely misconfigured must
        still fail with its own error: "cannot start a container" is a
        security-relevant fact and may not be retried into silence.
        """
        if self.mode == SandboxMode.LOCAL:
            self._started = True
            return

        cdp_port = self._allocate_port()
        self.cdp_port = cdp_port

        cmd = self._run_argv(workspace=self.workspace, cdp_port=cdp_port)
        attempts = start_retries() + 1
        first_error = ""

        for attempt in range(1, attempts + 1):
            try:
                proc = await asyncio.to_thread(
                    subprocess.run, cmd, capture_output=True, text=True, timeout=30
                )
            except subprocess.TimeoutExpired as e:
                raise RuntimeError("Sandbox start timed out") from e

            if proc.returncode == 0:
                self.container_id = proc.stdout.strip()[:64]
                self.display = ":0"  # Inside container
                self._started = True

                # Wait for Xvfb to be ready
                await asyncio.sleep(1)
                logger.info("Sandbox started: %s (CDP port %d)", self.container_id[:12], cdp_port)
                return

            stderr = proc.stderr.strip()
            if not first_error:
                first_error = stderr

            if attempt == attempts:
                # The LAST attempt's stderr is not necessarily the diagnosable
                # one — see _release_name below.
                detail = stderr
                if first_error and first_error != stderr:
                    detail = f"{stderr} (first attempt: {first_error})"
                logger.error("Failed to start sandbox: %s", detail)
                raise RuntimeError(f"Sandbox start failed: {detail}")

            delay = start_retry_seconds()
            logger.warning(
                "Sandbox start failed (attempt %d of %d), retrying in %.1fs: %s",
                attempt,
                attempts,
                delay,
                stderr,
            )
            await self._release_name()
            await asyncio.sleep(delay)

    async def _release_name(self) -> None:
        """Free `--name` before retrying, best effort.

        The retry reuses the identical argv, name and all. A runtime can create
        the container and THEN fail (network setup, the uid_map write, a hook),
        so attempt 2 dies on

            Error: creating container storage: the container name
            "sandbox-abc123" is already in use

        and the operator reads a name collision instead of the boot race that
        actually happened. Nothing here may raise: this runs on a path that is
        already failing, and a cleanup error must never replace the start
        error the caller is about to report.
        """
        try:
            await asyncio.to_thread(
                subprocess.run,
                [sandbox_binary(), "rm", "-f", self.container_name],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception as e:  # noqa: BLE001 - cleanup must not mask the start failure
            logger.debug("Could not remove %s before retrying: %s", self.container_name, e)

    async def exec(self, cmd: list[str], timeout: int = 30) -> dict[str, Any]:
        """Execute a command inside the sandbox.

        For LOCAL mode, runs via subprocess with DISPLAY set.
        For DOCKER mode, runs via docker exec.
        """
        if self.mode == SandboxMode.LOCAL:
            env = os.environ.copy()
            env["DISPLAY"] = self.display
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
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
            proc = await asyncio.to_thread(
                subprocess.run, docker_cmd, capture_output=True, text=True, timeout=timeout
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
            proc = await asyncio.to_thread(
                subprocess.run, cmd, capture_output=True, text=True, timeout=10
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
            await asyncio.to_thread(
                subprocess.run,
                [sandbox_binary(), "rm", "-f", str(self.container_id)],
                capture_output=True,
                timeout=15,
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
