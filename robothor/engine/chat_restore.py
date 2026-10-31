"""Webchat session restore at startup: read off the loop, apply on it.

Lifted out of ``chat.py`` (on the module-size ratchet) when the daemon began
reading sessions in a thread during its concurrent startup. The session cache
is shared with Telegram, Slack and inbound channels and is not thread-safe, so
the read and the apply are separate calls. Names are looked up on the ``chat``
module at call time, so the tests that patch ``chat.load_all_sessions`` keep
reaching this code.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from robothor.engine.models import PlanState

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig

logger = logging.getLogger(__name__)


def load_persisted_sessions(config: EngineConfig) -> dict[str, Any] | None:
    """Read every persisted webchat session. Blocking: call it in a thread.

    Only the read: the result is applied with :func:`apply_restored_sessions`
    on the event loop, because the session cache is shared with Telegram,
    Slack and inbound channels and is not safe to mutate from a worker thread.
    Returns None when the read fails (logged), as the old restore did.
    """
    from robothor.engine import chat

    try:
        sessions: dict[str, Any] = chat.load_all_sessions(
            limit_per_session=chat.MAX_HISTORY,
            tenant_id=config.tenant_id,
        )
        return sessions
    except Exception as e:
        logger.warning("Failed to load persisted webchat sessions: %s", e)
        return None


def apply_restored_sessions(sessions: dict[str, Any]) -> None:
    """Hydrate the shared session cache from :func:`load_persisted_sessions`.

    Runs on the event loop, before any channel can take a turn, so a message
    right after a restart sees its history rather than an empty session.
    """
    from robothor.engine import chat

    try:
        restored = 0
        for key, data in sessions.items():
            session = chat._get_session(key)
            history = data.get("history", [])
            if history:
                session.history = history
            model = data.get("model_override")
            if model:
                session.model_override = model
            # Hydrate pending plan if present and not expired
            plan_data = data.get("plan_state")
            if plan_data and isinstance(plan_data, dict):
                plan = PlanState(
                    plan_id=plan_data.get("plan_id", ""),
                    plan_text=plan_data.get("plan_text", ""),
                    original_message=plan_data.get("original_message", ""),
                    status=plan_data.get("status", "pending"),
                    created_at=plan_data.get("created_at", ""),
                    exploration_run_id=plan_data.get("exploration_run_id", ""),
                    approval_request_id=plan_data.get("approval_request_id", ""),
                    rejection_feedback=plan_data.get("rejection_feedback", ""),
                    plan_hash=plan_data.get("plan_hash", ""),
                    task_context=plan_data.get("task_context", {}),
                    creator_sender_info=plan_data.get("creator_sender_info"),
                    deep_plan=plan_data.get("deep_plan", False),
                    revision_count=plan_data.get("revision_count", 0),
                    revision_history=plan_data.get("revision_history", []),
                    execution_run_id=plan_data.get("execution_run_id", ""),
                )
                if plan.status == "pending" and not chat._plan_is_expired(plan):
                    session.active_plan = plan
                    logger.info("Restored pending plan %s for session %s", plan.plan_id, key)
            restored += 1
        if restored:
            logger.info("Restored %d chat sessions from DB", restored)
    except Exception as e:
        logger.warning("Failed to restore persisted webchat sessions: %s", e)
