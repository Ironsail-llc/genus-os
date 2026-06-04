"""Top-level interrupt + steer helpers (Rip 9).

Thin wrappers that look up an active session by ``run_id`` and call
the matching method. External callers (Telegram bot, web UI, REST
endpoint) shouldn't need to know about the registry indirection.
"""

from __future__ import annotations

import logging

from robothor.engine import session_registry

logger = logging.getLogger(__name__)


def interrupt_session(run_id: str, message: str | None = None) -> bool:
    """Request interrupt for the live session with ``run_id``.

    Returns ``True`` when a live session was found and the interrupt
    flag was set, ``False`` when no session matches (run already
    finished, never started, or registry was cleared by restart).
    """
    session = session_registry.lookup(run_id)
    if session is None:
        logger.debug("interrupt_session: no active session for run_id=%s", run_id)
        return False
    session.interrupt(message)
    return True


def steer_session(run_id: str, text: str) -> bool:
    """Inject ``text`` as a steer for the live session with ``run_id``."""
    session = session_registry.lookup(run_id)
    if session is None:
        logger.debug("steer_session: no active session for run_id=%s", run_id)
        return False
    session.steer(text)
    return True
