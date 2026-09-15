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

__all__ = ["harden_process"]

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
