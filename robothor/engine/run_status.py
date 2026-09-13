"""One door to a run's ``on_status`` callback, for code that is not the loop.

``AgentRunner.execute`` takes an ``on_status`` callback and hands it down to
``_run_loop``, which is where the engine's three lifecycle statuses
(``iteration_start``, ``tools_start``, ``tools_done``) are emitted. Two new
emitters are nowhere near that loop: the guardrail escalation path, which is
inside ``PermissionEscalationManager.request_approval`` several frames below a
tool call, and the ``ask_user`` handler, which runs inside the tool registry
with only a ``ToolContext``. Neither has ``on_status`` in scope, and threading
it to both would mean changing the tool-loop signature and two runner call
sites to serve a notification.

So the callback is registered under the run id for the life of the loop, and
the emitters look it up. The registry is deliberately in RAM: this is a
*transport*, not a record. The row in ``agent_questions`` (or the pending
``EscalationRequest``) is the truth about what was asked, and a status event
that reaches nobody must never be the reason an ask does not happen — which is
why :func:`emit_status` returns a bool and swallows everything.

Not a general event bus. One callback per run, registered by the runner and
cleared in the same ``finally`` that unregisters the session, so a sink cannot
outlive the SSE stream it writes to.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

    StatusSink = Callable[[dict[str, Any]], Awaitable[None]]

logger = logging.getLogger(__name__)

__all__ = [
    "emit_status",
    "register_status_sink",
    "reset_status_sinks",
    "status_sink_count",
    "unregister_status_sink",
]

_sinks: dict[str, Any] = {}


def register_status_sink(run_id: str, sink: Any) -> None:
    """Route this run's status events to ``sink`` until it is unregistered.

    A ``None`` sink registers nothing rather than a null object: most runs have
    no ``on_status`` at all, and an entry per run would make the "is anyone
    listening" question unanswerable.
    """
    if not run_id or sink is None:
        return
    _sinks[str(run_id)] = sink


def unregister_status_sink(run_id: str) -> None:
    """Stop routing. Safe for a run that never registered one."""
    _sinks.pop(str(run_id), None)


async def emit_status(run_id: str, event: dict[str, Any]) -> bool:
    """Deliver one status event. True only if a sink actually took it.

    Never raises. The caller is on the path of an approval prompt or a question
    to a person, and an SSE queue that has already been closed must not be the
    reason the person is never asked.
    """
    sink = _sinks.get(str(run_id))
    if sink is None:
        return False
    try:
        await sink(event)
    except Exception:  # noqa: BLE001 — a dead stream is not the emitter's problem
        logger.debug("status sink for run %s raised; dropping event", run_id, exc_info=True)
        return False
    return True


def status_sink_count() -> int:
    """How many runs currently have a listener. Introspection, not control."""
    return len(_sinks)


def reset_status_sinks() -> None:
    """Drop every sink. For tests and for a process reload."""
    with contextlib.suppress(Exception):
        _sinks.clear()
