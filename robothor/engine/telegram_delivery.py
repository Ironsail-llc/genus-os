"""Telegram delivery bookkeeping: what a turn records about its reply.

An interactive turn's reply is sent by ``TelegramBot._run_interactive``. Two
things about it live here, so the turn reads as the turn and each can be read
(and tested) on its own: whether the reply reached the chat
(``TelegramDeliveryMixin``), and what follows it in the background, saving the
exchange and then mapping the platform message ids to the saved rows
(``persist_and_map``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)
# The delivery mixin logs as the bot it is mixed into.
_bot_logger = logging.getLogger("robothor.engine.telegram")

# Delivery status written onto an interactive run when the Telegram send
# reported nothing sent. ``TelegramBot.send_message`` swallows per-chunk
# exceptions and returns an empty list on total failure, so "no messages"
# is the only signal a lost reply gives us.
INTERACTIVE_SEND_FAILED_STATUS = "failed: telegram send returned no messages"


class TelegramDeliveryMixin:
    """Records, on the run, whether its interactive reply was delivered."""

    async def _record_interactive_delivery(self, run: Any, sent: Any) -> None:
        """Record whether an interactive reply actually reached Telegram.

        ``delivery.deliver()`` is only wired into the hook, workflow and
        scheduler paths — it never runs for an interactive chat turn — so
        without this every ``trigger_type='telegram'`` run carried
        ``delivery_status IS NULL`` and a reply that never landed looked
        exactly like one that did.

        Truth comes from the sender's return value, not from reaching the
        line after the send: ``TelegramBot.send_message`` swallows per-chunk
        exceptions and returns one ``Message`` per delivered chunk (``[]``
        when every chunk failed). Same discipline as
        ``robothor.engine.alerts._send_telegram``'s ``delivered = bool(sent)``.

        Bookkeeping only — never raises, so a DB hiccup cannot turn a
        successful reply into an error for the operator.

        Args:
            run: The ``AgentRun`` this reply belongs to. ``None`` is a no-op
                (the run may not exist yet when an early failure replies).
            sent: Whatever ``TelegramBot.send_message`` returned.
        """
        if run is None:
            return
        try:
            delivered = bool(sent)
            run.delivery_status = "delivered" if delivered else INTERACTIVE_SEND_FAILED_STATUS
            run.delivered_at = datetime.now(UTC) if delivered else None
            run.delivery_channel = "telegram"
            if not delivered:
                _bot_logger.warning(
                    "Interactive Telegram reply reported no sent messages (run=%s)",
                    getattr(run, "id", None),
                )
            # Imported here (not at module scope) so the DB write stays a
            # lazy dependency and tests can patch it at its source module.
            from robothor.engine.delivery import _persist_delivery_status

            await _persist_delivery_status(run)
        except Exception as e:
            _bot_logger.warning("Interactive delivery bookkeeping failed: %s", e)


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
