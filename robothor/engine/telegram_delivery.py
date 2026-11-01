"""Telegram delivery bookkeeping: what a turn records after its reply went out.

An interactive turn's reply is sent by ``TelegramBot._run_interactive``; what
follows it — saving the exchange, then mapping the platform message ids to the
saved rows — runs in the background and lives here, so the turn reads as the
turn and this sequence can be read (and tested) on its own.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


async def persist_and_map(
    save_exchange_async: Callable[..., Awaitable[Any]],
    *,
    session_key: str,
    user_text: str,
    output_text: str | None,
    model: Any,
    tenant_id: str,
    user_extras: dict[str, Any] | None,
    user_message_id: str | None,
    chat_id: str,
    assistant_platform_ids: list[str],
    author_run_id: str | None,
) -> None:
    """Save one exchange, then link its inbound and outbound platform ids to the rows.

    ``save_exchange_async`` is passed in by the caller (``telegram`` resolves
    its own module-level name) so a test that patches it there still patches
    this write.
    """
    from robothor.engine.channel_bus import (
        record_inbound,
        record_outbound,
    )

    inserted = await save_exchange_async(
        session_key,
        user_text,
        output_text or "",
        channel="telegram",
        model_override=model,
        tenant_id=tenant_id,
        user_extras=user_extras,
    )
    if not inserted or len(inserted) < 2:
        return
    user_chat_message_id, assistant_chat_message_id = inserted

    loop = asyncio.get_running_loop()

    if user_message_id:
        try:
            await loop.run_in_executor(
                None,
                lambda: record_inbound(
                    tenant_id=tenant_id,
                    channel="telegram",
                    chat_id=chat_id,
                    platform_message_id=user_message_id,
                    session_key=session_key,
                    chat_message_id=user_chat_message_id,
                    author_agent_id="user",
                ),
            )
        except Exception as e:
            logger.debug("tg-map-inbound failed for %s: %s", chat_id, e)

    if assistant_platform_ids:
        try:
            await loop.run_in_executor(
                None,
                lambda: record_outbound(
                    tenant_id=tenant_id,
                    channel="telegram",
                    chat_id=chat_id,
                    platform_message_ids=assistant_platform_ids,
                    session_key=session_key,
                    chat_message_id=assistant_chat_message_id,
                    author_agent_id="main",
                    author_run_id=author_run_id,
                ),
            )
        except Exception as e:
            logger.debug("tg-map-outbound failed for %s: %s", chat_id, e)
