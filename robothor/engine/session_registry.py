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

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robothor.engine.session import AgentSession

logger = logging.getLogger(__name__)


_lock = threading.RLock()
_active: dict[str, AgentSession] = {}


def register(session: AgentSession) -> None:
    """Add a session to the registry, keyed by ``session.run_id``."""
    with _lock:
        _active[session.run_id] = session


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
