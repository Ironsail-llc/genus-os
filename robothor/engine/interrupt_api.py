"""Top-level interrupt + steer helpers (Rip 9).

Thin wrappers that look up an active session by ``run_id`` and call
the matching method. External callers (Telegram bot, web UI, REST
endpoint) shouldn't need to know about the registry indirection.
"""

from __future__ import annotations

import logging
from typing import Any

from robothor.engine import session_registry
from robothor.engine.sanitize import sanitize_log

logger = logging.getLogger(__name__)


def interrupt_session(run_id: str, message: str | None = None) -> bool:
    """Request interrupt for the live session with ``run_id``.

    Returns ``True`` when a live session was found and the interrupt
    flag was set, ``False`` when no session matches (run already
    finished, never started, or registry was cleared by restart).
    """
    session = session_registry.lookup(run_id)
    if session is None:
        logger.debug("interrupt_session: no active session for run_id=%s", sanitize_log(run_id))
        return False
    session.interrupt(message)
    _record_intervention(session, "interrupt", message)
    return True


def steer_session(run_id: str, text: str) -> bool:
    """Inject ``text`` as a steer for the live session with ``run_id``."""
    session = session_registry.lookup(run_id)
    if session is None:
        logger.debug("steer_session: no active session for run_id=%s", sanitize_log(run_id))
        return False
    session.steer(text)
    _record_intervention(session, "steer", text)
    return True


def _record_intervention(session: Any, kind: str, detail: str | None) -> None:
    """Persist the intervention as an operator signal (Phase 2). Fails soft."""
    try:
        from robothor.engine.operator_signals import record_intervention

        record_intervention(
            run_id=session.run.id,
            agent_id=session.run.agent_id,
            kind=kind,
            detail=detail,
            tenant_id=session.run.tenant_id,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "intervention record failed for run_id=%s: %s",
            sanitize_log(getattr(session, "run", None)),
            sanitize_log(exc),
        )
