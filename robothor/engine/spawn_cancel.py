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
import uuid
from contextvars import ContextVar, Token
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


#: The spawn frame that is currently inside ``runner.execute``. Read at
#: registration time by every installed ``ChildRunWatch``; only the one whose
#: token matches claims the session.
#:
#: Identity has to be POSITIONAL, not descriptive. ``spawn_agents`` gathers N
#: children and explicitly supports the same agent id more than once — the
#: wide-research pattern the dedup key was namespaced for. Matching on
#: (agent_id, parent_run_id) makes those siblings indistinguishable: the first
#: registration satisfies every unfilled watch, so one child is claimed twice
#: and the other is abandoned exactly as before the fix. Measured with two
#: concurrent same-agent children: 1 finalised, 1 abandoned.
#:
#: `asyncio.gather` runs each child in its own Task, and a Task copies the
#: context at creation, so a value set inside one child's frame is invisible
#: to its siblings. That is what makes this a position and not a global.
_child_token: ContextVar[str | None] = ContextVar("_child_token", default=None)


def current_child_token() -> str | None:
    """Which spawn frame is inside ``runner.execute`` right now, if any."""
    return _child_token.get()


class ChildRunWatch:
    """Capture the child's session as the runner announces it.

    The spawn path has to know WHICH run it abandoned, and it cannot learn
    that from ``runner.execute`` — the call never returns when it is
    cancelled. The runner announces every session it starts
    (``session_registry.announce`` / ``register``); this claims exactly the
    one carrying this frame's token.
    """

    def __init__(self, child_agent_id: str, parent_run_id: str) -> None:
        self._child_agent_id = child_agent_id
        self._parent_run_id = parent_run_id
        self.token = uuid.uuid4().hex
        self.session: AgentSession | None = None
        self._token_reset: Token[str | None] | None = None

    def _observe(self, session: AgentSession) -> None:
        if self.session is not None:
            return
        if current_child_token() != self.token:
            return
        self.session = session

    def __enter__(self) -> ChildRunWatch:
        from robothor.engine import session_registry

        session_registry.add_observer(self._observe)
        self._token_reset = _child_token.set(self.token)
        return self

    def __exit__(self, *exc: Any) -> None:
        from robothor.engine import session_registry

        if self._token_reset is not None:
            _child_token.reset(self._token_reset)
            self._token_reset = None
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


#: What this seam has actually done. A control that silently does nothing is
#: the failure this repo keeps rediscovering — six controls once shipped
#: built, wired, tested and inert. `unclaimed` is the dead-seam counter: a
#: cancellation reached the spawn path with no session to finalise, which
#: means the watch never heard about the child.
_STATS: dict[str, int] = {
    "cancellations": 0,
    "finalised": 0,
    "unclaimed": 0,
    "already_terminal": 0,
}


def finalisation_stats() -> dict[str, int]:
    """Snapshot of what the abandoned-child seam has done this process."""
    return dict(_STATS)


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
    _STATS["cancellations"] += 1
    if session is None:
        _STATS["unclaimed"] += 1
        logger.info(
            "Sub-agent cancellation from parent %s had no run to finalise "
            "(the child was never announced); seam totals: %s",
            parent_run_id,
            finalisation_stats(),
        )
        return None
    try:
        run = session.run
        if run.status not in (RunStatus.PENDING, RunStatus.RUNNING):
            # Some other frame — most likely the runner's own cancel arm —
            # already owns this row. Its account is the first-hand one.
            _STATS["already_terminal"] += 1
            logger.info(
                "Sub-agent run %s was already %s; leaving that account alone",
                run.id,
                run.status.value,
            )
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
        _STATS["finalised"] += 1
        # runner._finish_run is untyped at its definition; name the result so
        # mypy sees the declared return type rather than Any.
        finalised_run: AgentRun | None = runner._finish_run(
            finished,
            agent_config=agent_config,
            session=session,
            spawn_context=spawn_context,
        )
        return finalised_run
    except Exception as e:  # noqa: BLE001 - a cancellation is in flight
        logger.error("Failed to finalise abandoned sub-agent: %s", e)
        return None
