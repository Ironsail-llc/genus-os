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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.engine.code_execution import SandboxResult

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

__all__ = [
    "DRAIN_GRACE_SECONDS",
    "EXIT_POLL_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "DescendantCensus",
    "descendants_of",
    "kill_descendants",
    "kill_group",
    "run_snippet",
    "wait_for_exit",
]

#: The ceiling an agent cannot ask past, matching `exec`'s. A snippet that
#: outlives the run owning it is a leak, not a long job. It lives beside the
#: other two clocks rather than in the handler, because `tool_timeouts` reads
#: it to size the registry deadline that wraps this tool — and a ceiling the
#: outer bound has to import from a HANDLER is a dependency pointing the
#: wrong way.
MAX_TIMEOUT_SECONDS = 900

#: How often the exit poll wakes. Short enough that a snippet printing one line
#: does not feel slow, long enough that a fifteen-minute one costs a few
#: thousand cheap wakeups rather than a hundred thousand.
EXIT_POLL_SECONDS = 0.05

#: After the process group is killed, how long the pipes get to give up what
#: they still hold. Bounded because an orphan the kill could not reach (one
#: that changed its own group) would otherwise hold this call open.
DRAIN_GRACE_SECONDS = 5.0

#: The census rides the exit poll rather than a task of its own, and samples on
#: two cadences.
#:
#: A census rather than one sweep at kill time, because the two cases need
#: different evidence. On the timeout and cancellation paths the snippet is
#: still alive, so its parent chain is intact and one walk would do. On the
#: ORDINARY path it has already exited, its children have already reparented,
#: and the chain is gone — so what kills them is the union of what was seen
#: while it lived.
#:
#: Fast for the first second, because a snippet that spawns and exits does both
#: in well under one: fifteen `setsid` children and a `print` took 0.5 s, and a
#: half-second cadence caught none of them. Slow afterwards, because a
#: fifteen-minute snippet must not pay 18,000 procfs walks for a burst that
#: happened at startup.
FAST_SAMPLE_TICKS = 20
SLOW_SAMPLE_EVERY_TICKS = 10

#: ``PR_SET_CHILD_SUBREAPER``. The boot script sets it on the SNIPPET, not on
#: the engine, so a double-forked grandchild reparents to the snippet instead of
#: to init and stays on the chain the census walks. A snippet can undo it — it
#: is defence in depth, and it is what makes the honest case complete.
PR_SET_CHILD_SUBREAPER = 36


def _children_of(pid: int) -> list[int]:
    """The direct children of ``pid``, from procfs. [] when it is gone."""
    kids: list[int] = []
    try:
        tasks = Path(f"/proc/{pid}/task")
        for task in tasks.iterdir():
            with contextlib.suppress(OSError, ValueError):
                raw = (task / "children").read_text()
                kids.extend(int(part) for part in raw.split())
    except OSError:
        return []
    return kids


def descendants_of(pid: int) -> list[int]:
    """Every process below ``pid`` right now, deepest last.

    Breadth-first, and bounded: a fork bomb must not make this walk forever.
    The order matters at kill time — killing a parent first lets it fork again
    before its children die, so the caller reverses this.
    """
    seen: list[int] = []
    frontier = [pid]
    while frontier and len(seen) < MAX_TRACKED_DESCENDANTS:
        nxt: list[int] = []
        for parent in frontier:
            for kid in _children_of(parent):
                if kid > 1 and kid not in seen:
                    seen.append(kid)
                    nxt.append(kid)
        frontier = nxt
    return seen


#: Ceiling on the census. A snippet that has forked more than this has already
#: earned a kill, and the ones we did not enumerate are in the process group.
MAX_TRACKED_DESCENDANTS = 4096


class DescendantCensus:
    """What the snippet has started, as seen while it was still alive."""

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._seen: dict[int, None] = {}  # insertion-ordered set

    def sample(self) -> None:
        """One walk. Never raises: a missed sample is a smaller loss than a
        cancelled kill."""
        with contextlib.suppress(Exception):
            for kid in descendants_of(self._pid):
                self._seen.setdefault(kid, None)

    def sample_on_tick(self, tick: int) -> None:
        """Sample if this poll tick is due one. See the cadence constants."""
        if tick < FAST_SAMPLE_TICKS or tick % SLOW_SAMPLE_EVERY_TICKS == 0:
            self.sample()

    @property
    def known(self) -> list[int]:
        return list(self._seen)


def kill_descendants(census: DescendantCensus, pgid: int) -> int:
    """Kill everything the snippet started, then its group. Returns the count.

    Order is load-bearing twice. A final sample runs FIRST, so anything started
    since the last one is on the list. Then the list is killed deepest-first,
    because killing a parent before its children gives it the chance to fork
    again. The process group goes last and catches whatever the census could
    not see.

    What this does NOT guarantee, and the result text says so: a process that
    left the group AND detached itself from the snippet in the window between
    the last sample and the kill is reachable by neither path. Closing that
    needs a cgroup the engine can kill as a unit, which needs ``Delegate=yes``
    on the unit — an operator change, not a code one.
    """
    census.sample()
    killed = 0
    own_pgid = os.getpgid(0)
    for pid in reversed(census.known):
        if pid <= 1 or pid == os.getpid():
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGKILL)
            killed += 1
    if pgid > 1 and pgid != own_pgid:
        kill_group(pgid)
    return killed


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


async def wait_for_exit(proc: Any, timeout: float, census: DescendantCensus | None = None) -> bool:
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
    tick = 0
    while proc.returncode is None:
        # Before the deadline check, so a snippet that is about to be killed
        # still contributes one last census — the descendants it started are
        # the reason the kill is happening.
        if census is not None:
            census.sample_on_tick(tick)
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(EXIT_POLL_SECONDS)
        tick += 1
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
    # The census runs FROM THE START, not at kill time: on the ordinary exit
    # path the snippet is already gone when we kill, its children have already
    # reparented, and the only record of them is what was seen while it lived.
    census = DescendantCensus(proc.pid)

    timed_out = False
    try:
        timed_out = not await wait_for_exit(proc, timeout, census)
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
        kill_descendants(census, pgid)
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
