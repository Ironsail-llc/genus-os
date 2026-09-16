"""Spawning a snippet, bounding it, and making sure nothing survives it.

Separated from the handler because they answer different questions. The handler
decides whether this agent may run a snippet at all, stages the files and
shapes the result; everything here is about ONE process and its descendants —
how its output is read without deadlocking it, how its exit is detected without
waiting on an orphan, and how it is killed.

Three things in here were bugs in the first cut, each because two events that
look alike are not the same event:

* ``os.getpgid(pid)`` at KILL time raises once the child is reaped, so the
  suppression swallowed it and the orphan lived. The pgid is captured at spawn.
* ``await proc.wait()`` on an asyncio subprocess finishes only when the process
  has exited AND every pipe it handed out has closed — and a backgrounded child
  inherited stdout. Awaiting it held the tool call open for as long as the
  orphan ran. :func:`wait_for_exit` polls ``returncode``, which the child
  watcher sets on the process's own exit.
* The kill was a straight line rather than a ``finally``, so a cancelled
  handler — the registry deadline, the run watchdog, a workflow deadline —
  killed nothing at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from typing import TYPE_CHECKING, Any

from robothor.engine.code_execution import SandboxResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "DRAIN_GRACE_SECONDS",
    "EXIT_POLL_SECONDS",
    "kill_group",
    "run_snippet",
    "wait_for_exit",
]

#: How often the exit poll wakes. Short enough that a snippet printing one line
#: does not feel slow, long enough that a fifteen-minute one costs a few
#: thousand cheap wakeups rather than a hundred thousand.
EXIT_POLL_SECONDS = 0.05

#: After the process group is killed, how long the pipes get to give up what
#: they still hold. Bounded because an orphan the kill could not reach (one
#: that changed its own group) would otherwise hold this call open.
DRAIN_GRACE_SECONDS = 5.0


def kill_group(pgid: int) -> None:
    """Kill the snippet's whole process group. Never raises.

    `start_new_session=True` makes the child a session leader, so its process
    GROUP id equals its pid and everything it starts inherits that group. This
    is what reaches a `subprocess.Popen` the snippet left running after it
    returned — exactly the leak the `exec` handler warns the model about and
    had no way to enforce.

    The caller passes the pgid it captured at SPAWN time, never
    ``os.getpgid(pid)`` at kill time: once the child has been waited on it is
    reaped, `getpgid` raises, and the suppression below would swallow the
    failure and let the orphan live. That is how the first cut of this
    function passed its own test on the timeout path and failed it on the
    success path.

    The self-check is not paranoia about a value we computed — it is a check
    that ``start_new_session`` did what it said. If it ever silently did not,
    this would be the engine killing its own process group.
    """
    if pgid <= 0 or pgid == os.getpgid(0):
        logger.error("execute_code refused to kill process group %s — it is our own", pgid)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGKILL)


async def _drain(stream: Any, sink: list[bytes], hard_cap: int) -> None:
    """Read one pipe, stopping at the hard cap rather than at EOF.

    The cap is applied WHILE reading, not after. A snippet printing fifty
    megabytes must not get the engine to hold fifty megabytes first and decide
    afterwards that it was too much — buffering-then-capping is the hole the
    plugin scanner review recorded on 2026-09-15, in a different file.
    """
    held = 0
    while held < hard_cap:
        chunk = await stream.read(65536)
        if not chunk:
            return
        sink.append(chunk)
        held += len(chunk)
    # Past the cap: keep draining so the child is not blocked on a full pipe,
    # and keep none of it.
    while await stream.read(65536):
        pass


async def wait_for_exit(proc: Any, timeout: float) -> bool:
    """Wait for the SNIPPET to exit. True if it did, False on the deadline.

    Polls ``proc.returncode`` rather than awaiting ``proc.wait()``, and the
    difference is the whole point. asyncio's subprocess transport only finishes
    ``wait()`` once the process has exited AND every pipe it handed out has
    closed — and a child the snippet backgrounded inherited stdout, so it holds
    one open. Awaiting ``wait()`` therefore keeps the tool call alive for as
    long as the orphan runs, which is the opposite of the property this tool
    promises. ``returncode`` is set by the child watcher the moment the process
    itself exits, independent of any pipe.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while proc.returncode is None:
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(EXIT_POLL_SECONDS)
    return True


async def run_snippet(
    *,
    tools_dir: Path,
    workspace: Path,
    env: dict[str, str],
    timeout: int,
    hard_cap: int,
    on_spawn: Callable[[int], None] | None = None,
) -> SandboxResult:
    """Spawn, wait, and make sure nothing survives."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-B",
        str(tools_dir / "_boot.py"),
        cwd=str(workspace) if workspace.is_dir() else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    # Captured now, not at kill time: once the child is waited on it is reaped
    # and `os.getpgid` raises, which would leave anything it backgrounded
    # running while the kill looked like it had happened.
    pgid = proc.pid
    # Told to the socket BEFORE the next await, so there is no scheduling point
    # at which the server would accept a connection without knowing which
    # session may speak to it. `start_new_session=True` above makes this pid the
    # session id too.
    if on_spawn is not None:
        on_spawn(proc.pid)
    out: list[bytes] = []
    err: list[bytes] = []

    # The pipes are drained CONCURRENTLY with the wait, and the wait is on the
    # snippet's own exit — not on the pipes closing. Those are different
    # events, and conflating them is a bug in both directions:
    #
    #   * waiting for exit without draining deadlocks the moment the snippet
    #     prints more than the pipe buffer (64 KiB) and blocks on a reader that
    #     is not reading;
    #   * waiting for the pipes to CLOSE keeps this call alive for as long as
    #     anything the snippet backgrounded holds the inherited stdout — which
    #     is to say, a daemon the snippet started holds the tool call open. The
    #     first cut did that and its own "nothing survives" test passed for the
    #     wrong reason: the handler had simply waited for the orphan to finish.
    drains = asyncio.gather(
        _drain(proc.stdout, out, hard_cap),
        _drain(proc.stderr, err, hard_cap),
    )

    timed_out = False
    try:
        timed_out = not await wait_for_exit(proc, timeout)
    finally:
        # A `finally`, not a straight line, because the third way out of the
        # wait is CANCELLATION — `registry.execute` wraps every handler in
        # `asyncio.timeout`, the run watchdog cancels the task, a workflow
        # deadline fires. Without this the cancelled handler returned nothing
        # and left the snippet running on the host with no deadline at all,
        # its socket directory deleted from under it by the caller's own
        # cleanup. Measured: a registry-style cancel at 2 s left both the
        # snippet and its child alive indefinitely.
        #
        # It is also right on BOTH ordinary paths: a snippet that RETURNED
        # after backgrounding a child leaves that child behind, and "it
        # finished" is not the same as "nothing it started is still running".
        # Killing also closes the inherited pipe ends, which is what lets the
        # drains below reach EOF rather than wait on an orphan.
        kill_group(pgid)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(drains, timeout=DRAIN_GRACE_SECONDS)
        drains.cancel()
        with contextlib.suppress(Exception):
            await proc.wait()

    return SandboxResult(
        stdout=b"".join(out).decode("utf-8", errors="replace"),
        stderr=b"".join(err).decode("utf-8", errors="replace"),
        returncode=-1 if timed_out else (proc.returncode if proc.returncode is not None else -1),
        tool_call_count=0,
        timed_out=timed_out,
    )
