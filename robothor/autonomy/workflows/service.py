"""Independent protected browser service, reachable only over its private Unix socket."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import signal
import socket
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import uvicorn
from playwright.async_api import Playwright, async_playwright

from robothor.autonomy.store import AutonomyStore
from robothor.autonomy.worker import browser_environment, launch_local
from robothor.autonomy.workflows.api import create_app
from robothor.autonomy.workflows.manager import WorkflowManager
from robothor.settings import get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.async_api import Browser


class DriverStarter(Protocol):
    async def start(self) -> Playwright: ...


@contextlib.contextmanager
def service_socket(path: Path) -> Iterator[socket.socket]:
    """Hold an exclusive lifetime lease before removing any stale socket."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = path.parent.lstat()
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.getuid()
        or stat.S_IMODE(directory.st_mode) != 0o700
    ):
        raise PermissionError("workflow_runtime_directory_not_private")
    fd = os.open(path.parent / "broker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    listener = None
    bound = False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists() or path.is_symlink():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise PermissionError("workflow_socket_path_occupied")
            path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        bound = True
        path.chmod(0o600)
        listener.listen(64)
        listener.setblocking(False)
        yield listener
    finally:
        if listener:
            listener.close()
        if bound:
            path.unlink(missing_ok=True)
        os.close(fd)


async def start_driver(starter: DriverStarter) -> Playwright:
    """Called once, before serving or starting background tasks.

    Playwright's Node driver otherwise inherits the broker's database/signing
    environment. Chromium also receives an explicit allowlist on every launch.
    """
    parent_environment = dict(os.environ)
    clean = browser_environment()
    try:
        os.environ.clear()
        os.environ.update(clean)
        return await starter.start()
    finally:
        os.environ.clear()
        os.environ.update(parent_environment)


async def serve(manager: WorkflowManager, listener: socket.socket) -> None:
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGUSR1, manager.drain)
    loop.add_signal_handler(signal.SIGUSR2, manager.resume_admission)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(manager),
            access_log=False,
            log_config=None,
            log_level="critical",
            timeout_graceful_shutdown=5,
            limit_concurrency=32,
        )
    )

    async def reap() -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await manager.expire_idle()
                # A revoked grant or a flipped switch closes retained browsers
                # within one sweep instead of up to an hour.
                await manager.expire_unauthorized()
            except Exception:
                # The manager closes expired contexts even if journaling fails.
                # Never log Playwright exceptions containing page data.
                continue

    reaper = asyncio.create_task(reap())
    try:
        await server.serve(sockets=[listener])
    finally:
        loop.remove_signal_handler(signal.SIGUSR1)
        loop.remove_signal_handler(signal.SIGUSR2)
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper
        await manager.shutdown()


async def run(path: Path) -> None:
    with service_socket(path) as listener:
        driver = await start_driver(async_playwright())
        try:

            async def factory() -> Browser:
                return await launch_local(driver.chromium)

            await serve(WorkflowManager(AutonomyStore(), factory), listener)
        finally:
            await driver.stop()


def main() -> None:
    logging.disable(logging.CRITICAL)
    from robothor.engine.process_hardening import harden_process

    if not harden_process():
        raise SystemExit("workflow_process_isolation_unavailable")
    try:
        asyncio.run(run(Path(get_settings().autonomy.socket)))
    except Exception as exc:
        _report_startup_failure(exc)
        raise SystemExit("workflow_service_failed") from None


def _report_startup_failure(exc: BaseException) -> None:
    """Say WHAT went wrong, once, on the way out.

    Under ``Restart=always`` / ``RestartSec=5`` a persistent misconfiguration
    — a runtime directory that is not 0700, a socket already bound, no
    sandbox-capable Chromium — used to be an endless loop emitting the single
    word ``workflow_service_failed``. An operator cannot act on that, and the
    unit pages nobody, so the loop is invisible as well as useless.

    Two deliberate details:

    * ``logging.disable(CRITICAL)`` above is not lifted globally. This process
      drives a real browser over the owner's accounts and must never log page
      content; the one message that has to escape gets a local, temporary
      lift around this call and nothing else.
    * Only ``type(exc).__name__`` and ``str(exc)`` go out — never a traceback
      with local variables in it, which on this process could hold a resource
      value or a page fragment. An exception MESSAGE here is a path, a mode,
      an errno; that is what an operator needs and all they get.
    """
    previous = logging.root.manager.disable
    try:
        logging.disable(logging.NOTSET)
        logging.getLogger(__name__).error(
            "workflow_service_failed: %s: %s", type(exc).__name__, exc
        )
    except Exception:  # noqa: BLE001, S110 - the exit matters more than the message
        # Nothing to log to: logging is what just failed. The SystemExit
        # below still carries the name, and the unit still enters `failed`.
        pass
    finally:
        logging.disable(previous)


if __name__ == "__main__":
    main()
