"""Hand a finished exchange to conversation memory, from any chat channel.

Webchat and Telegram both call :func:`remember_exchange` after a turn. It
spawns conversation ingest (``robothor.memory.conversation_ingest``) in the
background; ingest extracts only the turns not yet extracted, so every
exchange, however short, is handed over. Here rather than in ``chat.py`` and
``telegram.py``: both are on the module-size ratchet.
"""

from __future__ import annotations

from typing import Any


def remember_exchange(
    *,
    session_key: str,
    history: list[dict[str, Any]],
    agent_id: str,
    channel: str,
    run_id: str,
    tenant_id: str,
    name: str,
) -> None:
    """Spawn conversation ingest for this session (fire-and-forget).

    ``tenant_id`` must be the tenant the exchange was saved under: ingest reads
    the stored turns back.
    """
    from robothor.engine.task_registry import get_task_registry
    from robothor.memory.conversation_ingest import ingest_conversation_session

    get_task_registry().spawn(
        ingest_conversation_session(
            session_key=session_key,
            history=list(history),
            agent_id=agent_id,
            trigger_type=channel,
            run_id=run_id,
            tenant_id=tenant_id,
        ),
        name=name,
    )
