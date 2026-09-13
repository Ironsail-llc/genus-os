"""The Telegram channel — a wrapper, not a rewrite.

``engine/telegram.py`` is the bot: polling, handlers, plan mode, chunked
``send_message``, the interactive reply path. None of it is touched here. This
module is the thin outbound adapter that lets ``deliver()`` reach that bot
*through a name* instead of through a hardcoded call, and it deliberately owns
no transport of its own: it sends through whatever was registered with
``register_platform_sender("telegram")``, so it works when the bot exists and
also when only a sender function was registered.

Why it delegates back into ``delivery``
---------------------------------------
``delivery._deliver_telegram`` is a public seam in practice. Tests import it and
call it directly, and instances monkeypatch it to intercept what gets sent. If
this wrapper sent on its own regardless, a replacement of that function would
silently stop intercepting — the exact shape of failure this codebase keeps
finding, where a control is correct and its caller is inert.

So: the real send lives in :meth:`TelegramChannel._send_now`, and
``_deliver_telegram`` is a thin bool-returning delegate to it. When
:meth:`TelegramChannel.send` sees that ``delivery._deliver_telegram`` is still
the original function, it calls ``_send_now`` directly and returns the
full-fidelity receipt. When it sees the attribute has been *replaced*, it honours
the replacement and derives the receipt from what that returned. The dependency
arrow inverts once Slack becomes a delivery target; until then every existing
assertion stays true.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from robothor.engine.channels.base import SendReceipt, receipt_from
from robothor.engine.chunking import split_telegram_message

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from robothor.engine.models import AgentConfig, AgentRun

logger = logging.getLogger(__name__)

__all__ = ["TelegramChannel"]


class TelegramChannel:
    """Outbound Telegram, reachable as ``delivery.channel: telegram``."""

    name = "telegram"

    #: The inbound half still lives in ``engine/telegram.py``. Declared and
    #: empty rather than filled with an abstraction that has one caller.
    inbound_router: Any | None = None

    async def start(self) -> None:
        """No-op: the bot's own lifecycle is owned by the daemon."""
        return

    async def stop(self) -> None:
        """No-op: see :meth:`start`."""
        return

    async def health(self) -> dict[str, Any]:
        from robothor.engine.delivery import get_platform_sender

        return {
            "channel": self.name,
            "sender_registered": get_platform_sender("telegram") is not None,
        }

    async def send(
        self,
        target: str,
        text: str,
        *,
        config: AgentConfig | None = None,
        run: AgentRun | None = None,
        **kw: Any,
    ) -> SendReceipt:
        """Send ``text`` and report only what the platform acknowledged.

        ``config`` supplies the display-name header and the chat id; ``run`` is
        needed only by the legacy delegate, which stamps it.
        """
        if config is None:
            return SendReceipt(
                acknowledged=0, expected=1, status="failed:telegram_no_config", target=target
            )

        from robothor.engine import delivery

        if delivery._deliver_telegram is delivery._ORIGINAL_DELIVER_TELEGRAM:
            return await self._send_now(config, text, target)

        if run is None:
            # The delegate has been replaced and there is no run for it to
            # record an outcome on. Sending around the replacement would defeat
            # the only reason this branch exists.
            return SendReceipt(
                acknowledged=0, expected=1, status="failed:telegram_no_run", target=target
            )
        return await self._send_through_replacement(config, text, target, run)

    async def _send_through_replacement(
        self, config: AgentConfig, text: str, target: str, run: AgentRun
    ) -> SendReceipt:
        """Hand off to whatever replaced ``delivery._deliver_telegram``.

        The replacement's **return value is not evidence**. A bare
        ``AsyncMock()`` returns a truthy ``MagicMock``, and accepting that as an
        acknowledged chunk would record ``delivered`` for a send that reached
        nobody — one level up from "the next line ran", which is the single rule
        :mod:`robothor.engine.channels.base` exists to enforce.

        What *is* evidence is the status the replacement wrote on the run: it is
        the same column ``deliver()`` would write, and a replacement that
        delegates to the real implementation gets one for free. A replacement
        that writes nothing is recorded ``failed:telegram_unproven`` — before
        this seam existed, such a patch left the column NULL, so inventing a
        ``delivered`` here would be strictly worse than the behaviour it
        replaced.

        ``post_delivery`` is False throughout: a replacement that reached the
        real send has already fired POST_DELIVERY from inside, and firing it
        again would write the operator's briefing into their own session twice —
        the second copy stripped of its header and its platform message ids.
        """
        from robothor.engine import delivery

        before = run.delivery_status
        await delivery._deliver_telegram(config, text, run)
        recorded = run.delivery_status

        if recorded and recorded != before:
            return SendReceipt(
                acknowledged=1 if recorded == "delivered" else 0,
                expected=1,
                status=recorded,
                target=target,
                body=text,
                post_delivery=False,
            )
        return SendReceipt(
            acknowledged=0,
            expected=1,
            status="failed:telegram_unproven",
            target=target,
            body=text,
            post_delivery=False,
        )

    async def _send_now(self, config: AgentConfig, text: str, target: str = "") -> SendReceipt:
        """Do the send. Every guard here caught a real production failure.

        Returns a receipt; stamping the run and firing POST_DELIVERY is the
        caller's job, so the channel bus keeps exactly one instrumentation
        point.
        """
        from robothor.engine.delivery import get_platform_sender

        sender = get_platform_sender("telegram")
        if sender is None:
            logger.warning("Telegram sender not initialized, can't deliver for %s", config.id)
            return SendReceipt(
                acknowledged=0, expected=1, status="failed:telegram_no_sender", target=target
            )

        chat_id = target or config.delivery_to
        if not chat_id:
            logger.warning("No delivery_to chat ID for %s", config.id)
            return SendReceipt(
                acknowledged=0, expected=1, status="failed:telegram_no_chat_id", target=chat_id
            )
        if "${" in chat_id:
            logger.error("Unexpanded env var in delivery_to for %s: %s", config.id, chat_id)
            return SendReceipt(
                acknowledged=0,
                expected=1,
                status="failed:telegram_unexpanded_chat_id",
                target=chat_id,
            )

        full_text = f"*{config.name}*\n\n{text}"
        expected_chunks = len(split_telegram_message(full_text))

        try:
            sent = await sender(chat_id, full_text)
        except Exception as e:
            logger.error("Telegram delivery failed for %s: %s", config.id, e)
            return SendReceipt(
                acknowledged=0,
                expected=expected_chunks,
                status=f"failed:telegram_exception: {e}",
                target=chat_id,
                body=full_text,
            )

        receipt = receipt_from(sent, expected_chunks, target=chat_id, body=full_text)
        if receipt.acknowledged == 0:
            logger.error(
                "Telegram delivery for %s acknowledged 0 of %d chunk(s) — the operator saw nothing",
                config.id,
                expected_chunks,
            )
        elif not receipt.complete:
            logger.error(
                "Telegram delivery for %s was truncated: %d of %d chunk(s) landed",
                config.id,
                receipt.acknowledged,
                expected_chunks,
            )
        return receipt

    async def ask(self, question: str, options: Sequence[str]) -> str:
        """Not implemented — see ``engine/permission_escalation.py``.

        A default answer here would be an approval nobody gave.
        """
        raise NotImplementedError(
            "interactive ask is not implemented for the Telegram channel yet; "
            "approval prompts still run through engine/permission_escalation.py"
        )

    async def resolve_identity(self, native_id: str) -> Any:
        """Not implemented — inbound identity still resolves in ``engine/telegram.py``."""
        raise NotImplementedError(
            "identity resolution is not implemented for the Telegram channel yet; "
            "the inbound path in engine/telegram.py still owns it"
        )
