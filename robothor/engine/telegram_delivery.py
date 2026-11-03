"""Delivery accounting for interactive Telegram replies.

Lifted out of ``telegram.py`` as its own mixin: whether a reply reached the
chat is a question about the send, not about the bot's message handling.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("robothor.engine.telegram")

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
                logger.warning(
                    "Interactive Telegram reply reported no sent messages (run=%s)",
                    getattr(run, "id", None),
                )
            # Imported here (not at module scope) so the DB write stays a
            # lazy dependency and tests can patch it at its source module.
            from robothor.engine.delivery import _persist_delivery_status

            await _persist_delivery_status(run)
        except Exception as e:
            logger.warning("Interactive delivery bookkeeping failed: %s", e)
