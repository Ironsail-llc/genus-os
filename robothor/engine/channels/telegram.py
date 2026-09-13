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

import asyncio
import contextvars
import logging
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT
from robothor.engine.channels import telegram_ask
from robothor.engine.channels.base import SendReceipt, receipt_from
from robothor.engine.chunking import split_telegram_message

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from robothor.engine.models import AgentConfig, AgentRun
    from robothor.identity import IdentityContext

logger = logging.getLogger(__name__)

__all__ = ["TelegramChannel"]

#: True while this task is inside ``TelegramChannel.send``. A ``ContextVar``
#: rather than an instance attribute because one channel object serves every
#: agent: two concurrent deliveries must not see each other's send as a
#: recursion, and a task-local flag is the only thing that gets that right.
_sending: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "robothor_telegram_channel_sending", default=False
)


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

        if _sending.get():
            # A replacement for `_deliver_telegram` that calls back into this
            # method would see itself installed and call itself again, forever.
            # Without this the symptom is a hung delivery or a blown stack, and
            # neither names the mistake.
            raise RuntimeError(
                "TelegramChannel.send was re-entered: a replacement for "
                "delivery._deliver_telegram routes back through "
                'get_channel("telegram").send. Call the original it replaced '
                "instead."
            )

        token = _sending.set(True)
        try:
            return await self._dispatch(target, text, config, run)
        finally:
            _sending.reset(token)

    async def _dispatch(
        self,
        target: str,
        text: str,
        config: AgentConfig,
        run: AgentRun | None,
    ) -> SendReceipt:
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
        that writes nothing is recorded ``failed:telegram_unproven``.

        That last one is a deliberate behaviour change, and the only one in this
        seam. Before the channel registry such a patch left the column NULL and
        ``_persist_delivery_status`` early-returns on a falsy status, so
        *nothing was recorded at all*; the row now carries a ``failed:``, which
        fires the heartbeat status ping and does not count toward
        ``analytics.py``'s delivered total. A recorded failure beats an
        unrecorded delivery, and inventing a ``delivered`` would be worse than
        either. Production's ``TelegramBot.send_message`` honours the list
        contract and the real delegate stamps the run, so this only reaches a
        replacement that does neither — see the contract stated in
        ``delivery._deliver_telegram``'s docstring and the status table in
        ``docs/SYSTEM_ARCHITECTURE.md``.

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

    async def ask(
        self,
        question: str,
        options: Sequence[str] = (),
        *,
        timeout: float = 300.0,
        target: str = "",
        addressee: str = "",
    ) -> str | None:
        """Put ``question`` to ``addressee`` at ``target`` and wait for an answer.

        ``target`` is the chat; ``addressee`` is the Telegram sender id of the
        person being asked, and the two together are what an answer has to match
        before it settles this ask — see
        :mod:`robothor.engine.channels.telegram_ask`. An empty ``addressee``
        means the question is the operator's, and only the owner gate can
        authorize a reply.

        ``None`` means nobody answered — no target, no reachable bot, or the
        clock ran out. It is never one of ``options``: a default answer here
        would be a decision nobody made, which is the whole reason
        ``channels/base.py`` says an unimplemented ``ask`` must raise rather
        than return something plausible.

        With options and a reachable aiogram ``Bot``, the question goes out as
        an inline keyboard whose ``callback_data`` carries the option INDEX
        (``ask:<id>:<n>``) — Telegram caps ``callback_data`` at 64 bytes, so an
        option longer than that would come back truncated as a *different*
        answer. Without a bot the options go out numbered in the text and a
        typed ``"1".."N"`` (or the option itself) answers it.
        """
        chat_id = str(target or "")
        if not chat_id:
            # Guessing a chat here would ask the wrong person. It is the same
            # judgement `_send_now` makes about a missing delivery_to.
            logger.warning("Telegram ask has no target chat; nobody can be asked")
            return None

        opts = tuple(str(o) for o in (options or ()))[: telegram_ask.MAX_ASK_OPTIONS]
        ask_id, future = telegram_ask.register_ask(
            chat_id=chat_id, sender_id=str(addressee or ""), options=opts
        )
        try:
            delivered_as = await telegram_ask.send_question(chat_id, question, ask_id, opts)
            if not delivered_as:
                logger.warning("Telegram ask %s was never delivered; not waiting", ask_id)
                return None
            # Only now is it known which inbound messages this ask may accept.
            # Until this call it accepts none, so a reply racing the send cannot
            # answer a keyboard question as though it were free text.
            telegram_ask.note_ask_delivery(ask_id, delivered_as)
            return await asyncio.wait_for(future, timeout=max(1.0, float(timeout)))
        except TimeoutError:
            logger.info("Telegram ask %s went unanswered for %ss", ask_id, timeout)
            return None
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a failed ask is an unanswered one
            logger.exception("Telegram ask %s failed", ask_id)
            return None
        finally:
            telegram_ask.discard_ask(ask_id)

    async def resolve_identity(
        self, native_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> IdentityContext | None:
        """Map a Telegram user id onto a Genus identity, or None.

        ``tenant_users`` stays the source of truth and ``lookup_user`` stays the
        reader: the whole inbound ladder in ``engine/telegram.py`` is built on
        that table, and a resolver here that answered out of
        ``user_channel_identities`` alone would hand back an identity the
        inbound path still treats as a stranger. So this delegates to
        :func:`robothor.identity.resolvers.resolve_identity`, which is what
        ``_resolve_telegram`` already does, and a pairing writes BOTH rows (see
        ``channels/identities.approve_pairing``) rather than teaching one of
        them to shadow the other.
        """
        from robothor.identity.resolvers import resolve_identity

        return await asyncio.to_thread(resolve_identity, self.name, native_id, tenant_id)
