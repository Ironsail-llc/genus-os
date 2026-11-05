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

__all__ = ["drop_inherited_credentials", "harden_process"]

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


def drop_inherited_credentials() -> tuple[str, ...]:
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

    A child that really needs one of these has it passed some other way:
    see ``calendar_recovery_worker``. Returns the names dropped, never a value.
    """
    import os

    from robothor.config import get_config
    from robothor.secrets.classification import is_platform_credential

    # First, so what it exports for this process is in `os.environ` already
    # and it has no reason to write it through to the C side again later.
    try:
        get_config()
    except Exception as exc:  # noqa: BLE001 - startup reports a bad config itself
        logger.debug("config not loaded before the drop (%s)", type(exc).__name__)

    names = tuple(sorted(name for name in os.environ if is_platform_credential(name)))
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
