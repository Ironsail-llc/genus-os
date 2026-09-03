"""Finalise a sub-agent that its parent's cancellation abandoned.

A sub-agent runs INLINE in its parent's asyncio task (``spawn.py`` awaits
``runner.execute`` directly, with no task of its own), under whatever deadline
the parent happens to be carrying — normally the 600s per-tool
``asyncio.timeout`` in ``tools/registry.py``. When that fires it cancels the
shared task. The parent converts the cancellation to a ``TimeoutError``, hands
its model "Tool spawn_agent timed out after 600s" and carries on. Nothing
finalises the child.

Measured 2026-09-03: 17 ``sub_agent`` rows carry ``reap_category`` values they
did not earn, and not one has a cancel diagnostic. Run 0a78ed9f sat `running`
for two hours after its last step, with NULL ``error_traceback``, before the
reaper tombstoned it and mislabelled the cause.

The investigation could not name the frame that diverted the
``CancelledError`` around the runner's own cancel handler — so this does not
depend on that handler running. It closes the invariant from the outside:
whichever frame routes the cancellation, the child's row is put through the
runner's finalisation path before the cancellation leaves ``spawn.py``.

Two design notes:

* ``finalize_abandoned_child`` is **synchronous**. Shielding an awaitable from
  the cancellation would have worked too, but a shield is still a suspension
  point with a budget around it; a plain function call inside
  ``except CancelledError`` cannot be interrupted at all.
* It never absorbs the cancellation. The caller re-raises. Catching a cancel
  and returning is how a 3600s benchmark ceiling became a suggestion in
  August; see the note at ``runner.py``'s cancel arm.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from robothor.engine.models import RunStatus

if TYPE_CHECKING:
    from collections.abc import Iterator

    from robothor.engine.models import AgentRun
    from robothor.engine.session import AgentSession

logger = logging.getLogger(__name__)

#: The event-loop time at which the innermost per-tool deadline expires, or
#: None when no tool deadline is in scope. Set by ``ToolRegistry.execute_tool``
#: and reset on the way out, so at any frame it names the deadline that frame
#: is actually running under. A nested tool call inside the child sets and
#: resets its own; by the time a cancellation unwinds back to the spawn frame,
#: what is left is the parent's.
_tool_deadline: ContextVar[float | None] = ContextVar("_tool_deadline", default=None)

#: Slack on the deadline comparison. A cancellation delivered by
#: ``asyncio.timeout`` is observed a scheduler tick or two after the deadline,
#: never before it — but clock reads either side of an ``await`` are not free,
#: so do not demand strict equality.
DEADLINE_GRACE_SECONDS = 0.05


@contextlib.contextmanager
def tool_deadline(timeout: float) -> Iterator[None]:
    """Record the deadline a tool call is running under, for its callees."""
    try:
        deadline = asyncio.get_running_loop().time() + timeout
    except RuntimeError:  # pragma: no cover - no loop means no deadline
        yield
        return
    token = _tool_deadline.set(deadline)
    try:
        yield
    finally:
        _tool_deadline.reset(token)


def parent_deadline_expired(grace: float = DEADLINE_GRACE_SECONDS) -> bool:
    """Did the enclosing per-tool deadline cause this cancellation?

    Evidence, not configuration: a deadline that is still in the future did
    not fire, and a cancellation arriving then came from somewhere else. That
    distinction is what keeps ``status='timeout'`` meaning something — see
    ``cancel_outcome.py`` for what conflating the two cost the timeout rate.
    """
    deadline = _tool_deadline.get()
    if deadline is None:
        return False
    try:
        now = asyncio.get_running_loop().time()
    except RuntimeError:  # pragma: no cover
        return False
    return now >= deadline - grace


class ChildRunWatch:
    """Capture the child's session as the runner registers it.

    The spawn path has to know WHICH run it abandoned, and it cannot learn
    that from ``runner.execute`` — the call never returns when it is
    cancelled. The runner registers every session it starts
    (``session_registry.register``); this listens for exactly one, filtered to
    the child that this spawn is waiting on.
    """

    def __init__(self, child_agent_id: str, parent_run_id: str) -> None:
        self._child_agent_id = child_agent_id
        self._parent_run_id = parent_run_id
        self.session: AgentSession | None = None

    def _observe(self, session: AgentSession) -> None:
        if self.session is not None:
            return
        run = getattr(session, "run", None)
        if run is None or run.agent_id != self._child_agent_id:
            return
        # parent_run_id is written before register(); a child that somehow
        # lacks it still matches on agent id, which is the only child this
        # frame can be waiting on.
        if run.parent_run_id and run.parent_run_id != self._parent_run_id:
            return
        self.session = session

    def __enter__(self) -> ChildRunWatch:
        from robothor.engine import session_registry

        session_registry.add_observer(self._observe)
        return self

    def __exit__(self, *exc: Any) -> None:
        from robothor.engine import session_registry

        session_registry.remove_observer(self._observe)


def _diagnostic(session: AgentSession, parent_run_id: str, elapsed_s: float) -> str:
    """What the NULL ``error_traceback`` should have said."""
    run = session.run
    return (
        f"child run {run.id} ({run.agent_id}) abandoned by parent {parent_run_id}\n"
        f"elapsed: {elapsed_s:.1f}s; steps recorded: {len(run.steps)}\n"
        f"last status before finalisation: {run.status.value}\n"
        "cause: the child is awaited inline in the parent's task, so the "
        "parent's cancellation reached it with no deadline of its own"
    )


def finalize_abandoned_child(
    runner: Any,
    session: AgentSession | None,
    *,
    parent_run_id: str,
    elapsed_s: float,
    agent_config: Any = None,
    spawn_context: Any = None,
) -> AgentRun | None:
    """Write the child's terminal row through the runner's finalisation path.

    Returns the finalised run, or None when there was nothing to finalise —
    the child never started, or a frame that got here first already wrote a
    terminal row. Never raises: this runs while a cancellation is in flight,
    and an exception here would replace the cancellation the caller must
    re-raise.
    """
    if session is None:
        return None
    try:
        run = session.run
        if run.status not in (RunStatus.PENDING, RunStatus.RUNNING):
            # Some other frame — most likely the runner's own cancel arm —
            # already owns this row. Its account is the first-hand one.
            return None

        seconds = int(elapsed_s)
        diag = _diagnostic(session, parent_run_id, elapsed_s)
        if parent_deadline_expired():
            reason = f"cancelled by parent {parent_run_id} tool timeout after {seconds}s"
            finished = session.timeout(reason=reason, traceback=diag)
        else:
            reason = f"cancelled by parent {parent_run_id} after {seconds}s"
            finished = session.cancelled(reason=reason, traceback=diag)

        logger.warning(
            "Sub-agent %s finalised as %s: %s",
            run.agent_id,
            finished.status.value,
            reason,
        )
        return runner._finish_run(
            finished,
            agent_config=agent_config,
            session=session,
            spawn_context=spawn_context,
        )
    except Exception as e:  # noqa: BLE001 - a cancellation is in flight
        logger.error("Failed to finalise abandoned sub-agent: %s", e)
        return None
