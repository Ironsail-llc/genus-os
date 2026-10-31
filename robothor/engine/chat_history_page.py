"""The page of chat history the ``/chat/history`` endpoint answers with.

Lives outside ``chat.py`` because that module is on a size ratchet, and
because it is a separate decision: where a page of history comes from.

Off (``ROBOTHOR_CHAT_HISTORY_PAGES`` unset), the in-memory tail, exactly as the
endpoint always answered: no times, no paging, no new keys. On, the page comes
from ``chat_messages``, which keeps every row with its ``created_at``; the
in-memory history is trimmed to ``MAX_HISTORY`` rows and stores no time, so it
can answer neither "when" nor "what came before".

The store read is a blocking psycopg2 call, so it runs in a worker thread
(engine rule 10). A first page the store cannot answer (no database, nothing
persisted yet, an error) falls back to the in-memory tail, so turning the flag
on never empties a chat. An older page never does: the tail is the NEWEST
rows, and returning it as "before" would show them twice.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from robothor.engine import chat_store
from robothor.engine.sanitize import sanitize_log

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)


def _pages_enabled() -> bool:
    from robothor.settings import get_settings

    return bool(get_settings().flags.chat_history_pages)


def _whole(value: str | None) -> int | None:
    """A non-negative whole number from the query string, or ``None``."""
    return int(value) if value is not None and value.isdigit() else None


async def history_page(
    history: list[Any], session_key: str, limit: int, query: Mapping[str, str], auth: Any
) -> dict[str, Any]:
    """``{"messages": [...]}``, plus ``hasMore`` when pages are on.

    ``query`` is the request's query string: ``offset`` and ``before`` (a
    ``chat_messages.id`` cursor) are read here, so ``chat.py`` keeps one line.
    """
    tail = history[-limit:] if limit > 0 else history
    if not _pages_enabled():
        return {"messages": tail}

    offset = _whole(query.get("offset")) or 0
    before = _whole(query.get("before"))
    older = offset > 0 or before is not None
    try:
        page = await asyncio.to_thread(
            chat_store.load_history_page,
            session_key,
            limit=limit,
            offset=offset,
            before=before,
            tenant_id=auth.tenant_id,
        )
    except Exception as e:
        logger.warning("Chat history page unavailable for %s: %s", sanitize_log(session_key), e)
        page = None
    if page is not None:
        return page
    return {"messages": [] if older else tail, "hasMore": False}
