"""Make the engine's own process unreadable to the agents it runs.

Scrubbing a child's environment removes INHERITANCE. It does not remove the
credentials from the engine, and on Linux ``/proc/<pid>/environ`` of a dumpable
process is readable by any process of the same uid — which every ``exec`` child
is, because ``subprocess.run(shell=True)`` makes the engine the parent of the
shell. So ``cat /proc/$PPID/environ`` read the whole decrypted secrets file back
out, from an agent running under ``enforce`` with no grants. Yama's
``ptrace_scope=1`` does not help: it restricts PTRACE_ATTACH, not the
PTRACE_MODE_READ that procfs uses.

``PR_SET_DUMPABLE=0`` is the actual fix. It changes the ownership of the
process's own ``/proc/<pid>`` entries to root, so ``environ``, ``maps``, ``mem``,
``cwd`` and ``fd`` stop being readable by a same-uid process. It is a kernel
boundary rather than a denylist, it costs nothing, and it is exactly what
``gpg-agent`` and ``ssh-agent`` do for the same reason.

What it costs: a core dump is no longer written for this process, and anything
that reads ``/proc/self/fd`` or ``/proc/self/maps`` from OUTSIDE the process
stops working. The process reading its own is unaffected (a process may always
read itself), which is what psutil-style self-monitoring does, so the engine's
own health checks are untouched.

This is a mitigation, not a boundary. The real remedy is that the engine's
environment stops holding application credentials at all — the SOPS shrink in
``docs/runbooks/SOPS_BOOTSTRAP.md``, after which procfs leaks only bootstrap
values. The boundary for an untrusted agent is the sandbox.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = [
    "LibpqOnlyPasswordError",
    "drop_inherited_credentials",
    "filter_playwright_driver_env",
    "harden_process",
]

#: ``PR_SET_DUMPABLE`` from ``<linux/prctl.h>``. Named rather than inlined so
#: the call site reads as what it does.
_PR_SET_DUMPABLE = 4


def harden_process() -> bool:
    """Make this process's ``/proc`` entries root-only. True when it took.

    Never raises. This runs at startup, before anything is serving, and a
    hardening step that can crash the engine is worse than no hardening step:
    the failure mode would be an instance that does not boot, traded for an
    exposure that requires local same-uid code execution to reach.

    Idempotent — calling it twice is a no-op, which matters because the daemon
    and the CLI can both reach it in one process.
    """
    try:
        import ctypes
        import ctypes.util
    except Exception:  # noqa: BLE001 - pragma: no cover - ctypes is always present
        return False

    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        result = libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
    except Exception as exc:  # noqa: BLE001 - not every platform has prctl
        logger.debug("process hardening unavailable (%s)", type(exc).__name__)
        return False

    if result != 0:
        logger.debug("prctl(PR_SET_DUMPABLE, 0) returned %s", result)
        return False

    logger.info(
        "process hardening: /proc entries for this process are now root-only, so an "
        "agent's shell command cannot read the engine's environment out of procfs"
    )
    return True


class LibpqOnlyPasswordError(RuntimeError):
    """The database password is set only where libpq, not the platform, reads it."""


def _refuse_a_libpq_only_password(config: object | None) -> None:
    """Fail before the drop rather than on the first connection after it.

    libpq reads ``PGPASSWORD`` and ``PGPASSFILE`` from the C environment, and
    :class:`robothor.config.DatabaseConfig` leaves the password out of the DSN
    when ``ROBOTHOR_DB_PASSWORD`` is empty, so libpq falls back to them. The
    drop empties the C side, so an engine authenticating that way over TCP
    would boot and then fail every connection.

    Over the local socket the config cannot say whether the password is used
    (peer authentication never reads it), so that case is a warning. With no
    config at all there is nothing to judge, and startup reports the config
    itself. Names only in every message, never a value.
    """
    import os

    if config is None:
        return
    db = getattr(config, "db", None)
    if db is None or str(getattr(db, "password", "") or ""):
        return
    present = [name for name in ("PGPASSWORD", "PGPASSFILE") if os.environ.get(name)]
    if not present:
        return
    named = f"{' and '.join(present)} {'is' if len(present) == 1 else 'are'} set"
    # The platform's host, or libpq's own when the platform names none: the DSN
    # leaves an empty host out, and libpq then reads PGHOST.
    host = str(getattr(db, "host", "") or "") or os.environ.get("PGHOST", "")
    if not host or host.startswith("/"):
        logger.warning(
            "%s but ROBOTHOR_DB_PASSWORD is empty. The engine removes libpq's own "
            "password variables from its environment at startup; over the local "
            "socket that is harmless with peer authentication, and fails every "
            "connection with password authentication. Set ROBOTHOR_DB_PASSWORD if "
            "the database needs one.",
            named,
        )
        return
    raise LibpqOnlyPasswordError(
        f"{named} but ROBOTHOR_DB_PASSWORD is empty and the database is reached over "
        "TCP. Set ROBOTHOR_DB_PASSWORD instead: the engine removes libpq's own "
        "password variables from its environment at startup, so the database would "
        "refuse every connection."
    )


#: The marker on the wrapper, so installing twice wraps once.
_FILTERED = "_robothor_without_platform_credentials"


def filter_playwright_driver_env() -> bool:
    """Start every Playwright driver in this process without the platform credentials.

    playwright-python spawns its Node driver with ``env=get_driver_env()``,
    which is a copy of ``os.environ``. ``os.environ`` keeps every value for this
    process's own reads after the startup drop, so the driver, which lives as
    long as the engine and whose environment its own uid can read, would hold
    them all. ``launch(env=...)`` only reaches the browser, not the driver.

    So the driver's environment builder is wrapped, once per process. The name
    is private to Playwright: if it moves, this says so in the log and returns
    False rather than pretending. Returns True when the filter is in place.
    """
    try:
        from playwright._impl import _transport
    except ImportError:
        return False
    current = getattr(_transport, "get_driver_env", None)
    if current is None:
        logger.warning(
            "playwright no longer exposes get_driver_env: its driver may inherit "
            "the platform's credentials"
        )
        return False
    if getattr(current, _FILTERED, False):
        return True

    from robothor.engine.exec_env import without_platform_credentials

    def get_driver_env() -> dict[str, str]:
        return without_platform_credentials(current())

    setattr(get_driver_env, _FILTERED, True)
    _transport.get_driver_env = get_driver_env  # type: ignore[attr-defined]
    return True


def drop_inherited_credentials(*, check_database_password: bool = True) -> tuple[str, ...]:
    """Stop this process's children inheriting the platform's own credentials.

    ``env=`` is optional on every spawn call in the standard library, and a
    child started without it inherits the process's C ``environ``. Reviewing
    each call would hold for as long as nobody adds one, so this empties the
    C ``environ`` of what no child may have, once, at startup.

    ``os.environ`` is a Python copy made at interpreter start, and removing a
    name from the C side alone leaves it there, so every read in this process
    keeps working. The cost is the reverse of that: a child handed a copy of
    ``os.environ`` gets everything back, which ``build_exec_env`` and
    ``without_platform_credentials`` are for, and which
    ``test_exec_env_platform_credentials.py`` refuses in an engine module.
    Playwright's driver makes that copy inside a third-party package, so
    :func:`filter_playwright_driver_env` is installed here as well.

    A child that really needs one of these has it passed some other way:
    see ``calendar_recovery_worker``. Returns the names dropped, never a value.

    Raises :class:`LibpqOnlyPasswordError` when the database is reached over
    TCP and its password is set only in ``PGPASSWORD`` or ``PGPASSFILE``: the
    drop would take it from libpq.
    ``check_database_password=False`` skips that check, for a child of an
    engine that has already made it at its own startup.
    """
    import os

    from robothor.config import get_config
    from robothor.secrets.classification import is_platform_credential

    # First, so what it exports for this process is in `os.environ` already
    # and it has no reason to write it through to the C side again later.
    config = None
    try:
        config = get_config()
    except Exception as exc:  # noqa: BLE001 - startup reports a bad config itself
        logger.debug("config not loaded before the drop (%s)", type(exc).__name__)
    if check_database_password:
        _refuse_a_libpq_only_password(config)

    filter_playwright_driver_env()

    names = tuple(
        sorted(name for name, value in os.environ.items() if is_platform_credential(name, value))
    )
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        for name in names:
            libc.unsetenv(name.encode())
    except Exception as exc:  # noqa: BLE001 - never a reason not to boot, always a reason to say
        logger.warning(
            "could not drop inherited credentials (%s): children of this process "
            "that pass no environment keep them",
            type(exc).__name__,
        )
        return ()
    return names
