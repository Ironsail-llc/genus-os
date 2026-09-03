"""In-process registry of live AgentSession objects (Rip 9).

The interrupt/steer APIs need to look up a session by ``run_id``
from outside the runner (e.g. the Telegram bot receiving a follow-up
message while the agent is mid-turn). The registry is intentionally
in-memory only — sessions don't survive process restart, and an
interrupt that arrives after the run finished is a no-op (the lookup
returns ``None``).

Thread/asyncio-safe via a single module-level dict and atomic
``dict.pop`` semantics. The runner registers a session at the start
of ``execute`` and unregisters in a ``finally`` so the registry can
never accumulate stale entries.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.session import AgentSession

logger = logging.getLogger(__name__)


_lock = threading.RLock()
_active: dict[str, AgentSession] = {}

#: Called with every session as it registers. The spawn path uses this to
#: learn WHICH run its child got, so it can finalise that row if the parent's
#: cancellation abandons the child mid-flight (see spawn_cancel.py). A
#: callback that raises is dropped on the floor: observing a registration may
#: never be the reason a run fails to start.
_observers: list[Callable[[AgentSession], None]] = []


def add_observer(fn: Callable[[AgentSession], None]) -> None:
    """Watch registrations until ``remove_observer``. Not for long-lived use."""
    with _lock:
        _observers.append(fn)


def remove_observer(fn: Callable[[AgentSession], None]) -> None:
    """Stop watching. Idempotent."""
    with _lock:
        with contextlib.suppress(ValueError):
            _observers.remove(fn)


def announce(session: AgentSession) -> None:
    """Tell observers a run exists, WITHOUT adding it to the live registry.

    `register` happens ~275 lines into `AgentRunner.execute`, after prompt
    assembly, the planner call and sandbox start — that is the right moment
    for the interrupt/steer registry, which only means anything once the loop
    is running. It is far too late for the spawn path's cancellation watch: a
    child cancelled anywhere in that stretch leaves the incident's exact
    signature (`running`, NULL traceback, zero steps) with nothing to finalise
    it. `AgentSession.start` announces, so the window closes at the point the
    run actually begins.

    Announcing does not register: nothing here can leak an entry into
    `_active` for a run whose loop never started.
    """
    with _lock:
        watchers = list(_observers)
    for fn in watchers:
        try:
            fn(session)
        except Exception as e:  # noqa: BLE001 - an observer must not break a run
            logger.warning("session_registry observer failed: %s", e)


def register(session: AgentSession) -> None:
    """Add a session to the registry, keyed by ``session.run_id``."""
    with _lock:
        _active[session.run_id] = session
    announce(session)


def unregister(session_or_run_id: AgentSession | str) -> None:
    """Remove a session from the registry. Idempotent."""
    run_id = session_or_run_id if isinstance(session_or_run_id, str) else session_or_run_id.run_id
    with _lock:
        _active.pop(run_id, None)


def lookup(run_id: str) -> AgentSession | None:
    """Return the live session for ``run_id``, or ``None`` if not active."""
    with _lock:
        return _active.get(run_id)


def active_count() -> int:
    """Diagnostic: number of currently-active sessions."""
    with _lock:
        return len(_active)


def active_run_ids() -> list[str]:
    """Snapshot of currently-active run_ids (for debugging surfaces)."""
    with _lock:
        return list(_active.keys())
