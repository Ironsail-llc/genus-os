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
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from robothor.engine.channels.base import SendReceipt, acknowledged_messages, receipt_from
from robothor.engine.chunking import split_telegram_message

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from robothor.engine.models import AgentConfig, AgentRun

logger = logging.getLogger(__name__)

__all__ = [
    "TelegramChannel",
    "has_pending_ask",
    "pending_ask_ids",
    "reset_pending_asks",
    "resolve_ask_choice",
    "resolve_ask_text",
]

#: Telegram renders more than this as a wall of buttons, and an operator
#: scrolling a keyboard is an operator who taps the wrong one.
MAX_ASK_OPTIONS = 6

#: True while this task is inside ``TelegramChannel.send``. A ``ContextVar``
#: rather than an instance attribute because one channel object serves every
#: agent: two concurrent deliveries must not see each other's send as a
#: recursion, and a task-local flag is the only thing that gets that right.
_sending: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "robothor_telegram_channel_sending", default=False
)


# ─── Pending asks ───────────────────────────────────────────────────
#
# Module-level rather than an attribute of ``TelegramChannel``: the object that
# asks is built by the registry on demand, and the object that hears the answer
# is the bot's dispatcher in ``engine/telegram.py``. Two instances with two
# dicts would be an ask nobody could ever resolve.
#
# The registry lives here and not in ``engine/telegram.py`` for a second reason
# as well: that module is 100 lines from its size ratchet, and the half of this
# feature that belongs to the *channel* should be findable beside the channel.


@dataclass
class _PendingAsk:
    """One question waiting on a person, and where it was asked."""

    future: asyncio.Future[str | None]
    chat_id: str
    options: tuple[str, ...]


#: ask_id → the question waiting on it. One process, one dict.
_pending_asks: dict[str, _PendingAsk] = {}


def pending_ask_ids() -> list[str]:
    """The ids of questions currently waiting. Introspection, not control."""
    return list(_pending_asks)


def has_pending_ask(chat_id: str) -> bool:
    """Whether a free-text question is waiting on an answer from ``chat_id``.

    Only free-text asks count. A question offered as buttons is answered by
    tapping one: treating the next line the operator types as its answer would
    turn "hang on, what was the second option again?" into a decision.
    """
    return any(p.chat_id == str(chat_id) and not p.options for p in _pending_asks.values())


def resolve_ask_choice(ask_id: str, index: int) -> str | None:
    """Answer a keyboard ask with the option at ``index``. Returns the option.

    ``None`` for an unknown id, an out-of-range index, or an ask that has
    already been answered — every one of which is an ordinary race (a
    double-tap, a button pressed after the tool gave up) rather than an error.
    """
    pending = _pending_asks.get(str(ask_id))
    if pending is None or pending.future.done():
        return None
    if not 0 <= index < len(pending.options):
        return None
    answer = pending.options[index]
    pending.future.set_result(answer)
    return answer


def resolve_ask_text(chat_id: str, text: str) -> bool:
    """Answer the oldest free-text ask waiting on ``chat_id`` with ``text``.

    True only when this call is what answered a question — the caller uses that
    to decide whether the message was consumed or should go on to the run.
    """
    for ask_id, pending in list(_pending_asks.items()):
        if pending.chat_id != str(chat_id) or pending.options or pending.future.done():
            continue
        pending.future.set_result(text)
        logger.debug("Telegram ask %s answered with free text", ask_id)
        return True
    return False


def reset_pending_asks() -> None:
    """Drop every pending ask. For tests and for a process reload."""
    for pending in list(_pending_asks.values()):
        if not pending.future.done():
            pending.future.cancel()
    _pending_asks.clear()


def _raw_bot() -> Any | None:
    """The aiogram ``Bot`` behind the registered Telegram sender, or None.

    The sender is ``TelegramBot.send_message``, and that wrapper silently drops
    ``reply_markup`` — sending a keyboard through it produces a prompt with no
    buttons and a plausible-looking success. ``permission_escalation`` unwraps
    the same way and says the same thing; this is the second caller, not a
    second opinion.

    ``None`` when the registered sender is a bare function (no bot to unwrap),
    which is a real configuration: the ask then goes out as plain text.
    """
    from robothor.engine.delivery import get_platform_sender

    sender = get_platform_sender("telegram")
    return getattr(getattr(sender, "__self__", None), "bot", None)


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
    ) -> str | None:
        """Put ``question`` to the person at ``target`` and wait for an answer.

        ``None`` means nobody answered — no target, no reachable bot, or the
        clock ran out. It is never one of ``options``: a default answer here
        would be a decision nobody made, which is the whole reason
        ``channels/base.py`` says an unimplemented ``ask`` must raise rather
        than return something plausible.

        With options, the question goes out as an inline keyboard whose
        ``callback_data`` carries the option INDEX (``ask:<id>:<n>``). Telegram
        caps ``callback_data`` at 64 bytes, so an option longer than that would
        come back truncated — as a different answer, silently. Without options
        the question is plain text and ``handle_text`` intercepts the reply.
        """
        chat_id = str(target or "")
        if not chat_id:
            # Guessing a chat here would ask the wrong person. It is the same
            # judgement `_send_now` makes about a missing delivery_to.
            logger.warning("Telegram ask has no target chat; nobody can be asked")
            return None

        opts = tuple(str(o) for o in (options or ()))[:MAX_ASK_OPTIONS]
        ask_id = uuid.uuid4().hex
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        _pending_asks[ask_id] = _PendingAsk(future=future, chat_id=chat_id, options=opts)
        try:
            if not await self._send_question(chat_id, question, ask_id, opts):
                logger.warning("Telegram ask %s was never delivered; not waiting", ask_id)
                return None
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
            _pending_asks.pop(ask_id, None)

    async def _send_question(
        self, chat_id: str, question: str, ask_id: str, options: tuple[str, ...]
    ) -> bool:
        """Put the question on the wire. False when nothing was acknowledged.

        False is load-bearing: :meth:`ask` refuses to block on a question that
        was never delivered, because an agent waiting ten minutes for a message
        nobody received is worse than one told immediately that it is on its
        own.
        """
        raw = _raw_bot() if options else None
        if raw is not None:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text=text, callback_data=f"ask:{ask_id}:{index}")
                        for index, text in enumerate(options)
                    ]
                ]
            )
            try:
                sent = await raw.send_message(chat_id, question, reply_markup=keyboard)
            except Exception:  # noqa: BLE001
                logger.exception("Telegram ask %s could not be sent", ask_id)
                return False
            return sent is not None

        from robothor.engine.delivery import get_platform_sender

        sender = get_platform_sender("telegram")
        if sender is None:
            logger.warning("Telegram sender not initialized; nobody can be asked")
            return False

        body = question
        if options:
            # No raw bot to attach a keyboard to, so the options have to be in
            # the text or they are not offered at all.
            body += "\n\n" + "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
        try:
            sent = await sender(chat_id, body)
        except Exception:  # noqa: BLE001
            logger.exception("Telegram ask %s could not be sent", ask_id)
            return False
        # The length of the returned list is the only evidence anybody saw it —
        # see the rule in ``channels/base.py``.
        acknowledged, _ = acknowledged_messages(sent)
        return acknowledged > 0

    async def resolve_identity(self, native_id: str) -> Any:
        """Not implemented — inbound identity still resolves in ``engine/telegram.py``."""
        raise NotImplementedError(
            "identity resolution is not implemented for the Telegram channel yet; "
            "the inbound path in engine/telegram.py still owns it"
        )
