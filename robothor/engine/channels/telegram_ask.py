"""Asking a person on Telegram, and deciding whose answer counts.

``TelegramChannel`` is an outbound adapter; this is the half that waits. It
holds the pending questions, puts them on the wire, and — the part that took two
attempts to get right — decides which inbound message is allowed to settle
which one.

The binding
-----------
An ask is bound at mint to ``(chat_id, addressee sender_id)`` and an answer
settles it only when it arrives from that chat AND that sender. The first cut
authorized purely through ``TelegramBot._check_owner_gate``, whose default mode
is ``chat_id == default_chat_id``, and that was wrong in both directions at
once: the person a question was addressed to could not answer it unless their
chat happened to be the operator's, and anybody authorized in the operator's
chat could settle an ask registered to a different chat, because nothing
compared the two. The ask id travels in ``callback_data``, so "nothing compared
the two" is the whole exploit.

The owner gate did not go away. It is an **additional** requirement for an ask
raised in the operator's own chat, and the **only** authorization for an ask
with no addressee — which is every permission escalation, raised for the
operator by construction.

A refusal is silent to whoever sent it and counted in the log. Telling an
unauthorized caller which of the three checks they failed is telling them how to
pass it.

Its own module rather than more of ``channels/telegram.py`` for two reasons: the
outbound send adapter and the inbound answer policy are different jobs, and
``engine/telegram_handlers.py`` is at its size ratchet, so the handler bodies
have to live somewhere that is not it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from robothor.engine.channels.base import acknowledged_messages

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ASK_OPTIONS",
    "handle_ask_callback",
    "intercept_ask_answer",
    "pending_ask_ids",
    "register_ask",
    "reset_pending_asks",
    "resolve_ask_choice",
    "resolve_ask_text",
    "send_question",
]

#: Telegram renders more than this as a wall of buttons, and an operator
#: scrolling a keyboard is an operator who taps the wrong one.
MAX_ASK_OPTIONS = 6

#: What an unauthorized answer is told. Deliberately the same sentence for a
#: wrong chat, a wrong sender and a failed owner gate.
_REFUSED = "That question is not yours to answer"

#: What a tap on a question that is no longer open is told.
STALE_ASK = "That question is no longer open"


@dataclass
class _PendingAsk:
    """One question waiting on a person: who was asked, where, and how.

    ``sender_id`` empty means *no addressee* — the ask was raised for the
    operator (every escalation is) and only the owner gate can authorize it.

    ``delivered_as`` is ``"keyboard"`` or ``"text"``. It is not cosmetic: a
    keyboard ask is answered by tapping, and treating the next line the operator
    types as its answer would turn "hang on, what was the second option again?"
    into a decision. A *text* ask with options — the fallback when no aiogram
    ``Bot`` is reachable — has no buttons to tap, so it must accept a typed
    reply or it can only ever time out.

    ``message_id`` is the platform id of the question, when the send returned
    one. It is what lets a quoted reply answer the question it quotes rather
    than whichever question has been waiting longest.
    """

    future: asyncio.Future[str | None]
    chat_id: str
    sender_id: str
    options: tuple[str, ...]
    delivered_as: str = "text"
    message_id: str = ""

    @property
    def takes_free_text(self) -> bool:
        return self.delivered_as == "text"


#: ask_id → the question waiting on it. Module level rather than an attribute of
#: ``TelegramChannel`` because the object that asks is built by the registry on
#: demand and the object that hears the answer is the bot's dispatcher: two
#: instances with two dicts would be an ask nobody could ever resolve.
_pending_asks: dict[str, _PendingAsk] = {}


def register_ask(
    *,
    chat_id: str,
    sender_id: str = "",
    options: tuple[str, ...] = (),
    delivered_as: str = "text",
    message_id: str = "",
) -> tuple[str, asyncio.Future[str | None]]:
    """Mint a pending ask and return ``(ask_id, future)``.

    Minting before the send is deliberate: the ``callback_data`` has to carry
    the id, and a keyboard whose id is not yet registered is a button that does
    nothing.
    """
    ask_id = uuid.uuid4().hex
    future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    _pending_asks[ask_id] = _PendingAsk(
        future=future,
        chat_id=str(chat_id),
        sender_id=str(sender_id or ""),
        options=tuple(options),
        delivered_as=delivered_as,
        message_id=str(message_id or ""),
    )
    return ask_id, future


def discard_ask(ask_id: str) -> None:
    """Forget one ask. The asking coroutine calls this in its ``finally``."""
    _pending_asks.pop(str(ask_id), None)


def note_ask_message_id(ask_id: str, message_id: Any) -> None:
    """Record the platform id of the question, once the send returns one."""
    pending = _pending_asks.get(str(ask_id))
    if pending is not None and message_id is not None:
        pending.message_id = str(message_id)


def pending_ask_ids() -> list[str]:
    """The ids of questions currently waiting. Introspection, not control."""
    return list(_pending_asks)


def reset_pending_asks() -> None:
    """Drop every pending ask. For tests and for a process reload."""
    for pending in list(_pending_asks.values()):
        if not pending.future.done():
            pending.future.cancel()
    _pending_asks.clear()


# ─── Authorization ──────────────────────────────────────────────────


def _authorized(
    pending: _PendingAsk, *, chat_id: str, sender_id: str, owner_ok: bool, operator_chat: bool
) -> bool:
    """Whether this (chat, sender) may settle this ask. See the module docstring."""
    if pending.chat_id != str(chat_id):
        return False
    if not pending.sender_id:
        # No addressee: raised for the operator, so the gate is all there is.
        return owner_ok
    if pending.sender_id != str(sender_id):
        return False
    # A bound addressee answering in their own chat needs nothing further; in
    # the operator's chat the role-gate ladder still applies on top.
    return owner_ok or not operator_chat


def _refuse(ask_id: str, chat_id: str) -> None:
    """Count a refused answer. Identifiers only, and never the ask's binding."""
    logger.warning("ask_answer refused: ask=%s from_chat=%s", ask_id, chat_id)


# ─── Resolution ─────────────────────────────────────────────────────


def resolve_ask_choice(
    ask_id: str,
    index: int,
    *,
    chat_id: str,
    sender_id: str,
    owner_ok: bool,
    operator_chat: bool = False,
) -> str | None:
    """Answer a keyboard ask with the option at ``index``. Returns the option.

    ``None`` for an unknown id, an out-of-range index, an ask already answered,
    or an answer from somebody not bound to it. The first three are ordinary
    races (a double-tap, a button pressed after the tool gave up); the last is
    refused and counted.
    """
    pending = _pending_asks.get(str(ask_id))
    if pending is None or pending.future.done():
        return None
    if not _authorized(
        pending,
        chat_id=chat_id,
        sender_id=sender_id,
        owner_ok=owner_ok,
        operator_chat=operator_chat,
    ):
        _refuse(str(ask_id), str(chat_id))
        return None
    if not 0 <= index < len(pending.options):
        return None
    answer = pending.options[index]
    pending.future.set_result(answer)
    return answer


def _text_answer(pending: _PendingAsk, text: str) -> str | None:
    """What ``text`` means for this ask, or None if it does not answer it.

    A free-text ask takes the text. An options ask delivered as numbered text
    (no keyboard was attachable) takes ``"1".."N"`` or the option itself —
    matched exactly first, then case-insensitively, because an operator typing
    an answer types it the way they would say it.
    """
    body = text.strip()
    if not pending.options:
        return body
    if body.isdigit():
        index = int(body) - 1
        return pending.options[index] if 0 <= index < len(pending.options) else None
    for option in pending.options:
        if body == option:
            return option
    for option in pending.options:
        if body.casefold() == option.casefold():
            return option
    return None


def resolve_ask_text(
    chat_id: str,
    sender_id: str,
    text: str,
    *,
    owner_ok: bool,
    operator_chat: bool = False,
    reply_to_message_id: str = "",
) -> bool:
    """Answer a pending ask from ``chat_id`` with ``text``.

    True only when this call is what answered a question — the caller uses that
    to decide whether the message was consumed or should go on to the run.

    When the reply quotes the question (``reply_to_message_id``), that ask is
    the one answered. Otherwise the **oldest** pending ask this sender may
    answer wins, which is a documented tie-break rather than a guess: with
    nothing to correlate on, the question that has been waiting longest is the
    one being answered.
    """
    candidates = [
        (ask_id, pending)
        for ask_id, pending in _pending_asks.items()
        if pending.takes_free_text
        and not pending.future.done()
        and _authorized(
            pending,
            chat_id=chat_id,
            sender_id=sender_id,
            owner_ok=owner_ok,
            operator_chat=operator_chat,
        )
    ]
    if reply_to_message_id:
        quoted = [c for c in candidates if c[1].message_id == str(reply_to_message_id)]
        # A quote that names an ask this sender may not answer is not a reason
        # to fall back to one they may — it is a reason to leave it alone.
        candidates = quoted

    for ask_id, pending in candidates:
        answer = _text_answer(pending, text)
        if answer is None:
            continue
        pending.future.set_result(answer)
        logger.debug("Telegram ask %s answered with text", ask_id)
        return True
    return False


# ─── Outbound ───────────────────────────────────────────────────────


def _raw_bot() -> Any | None:
    """The aiogram ``Bot`` behind the registered Telegram sender, or None.

    The sender is ``TelegramBot.send_message``, and that wrapper silently drops
    ``reply_markup`` — sending a keyboard through it produces a prompt with no
    buttons and a plausible-looking success. ``permission_escalation`` unwraps
    the same way and says the same thing; this is the second caller, not a
    second opinion.

    ``None`` when the registered sender is a bare function (no bot to unwrap),
    which is a real configuration: the ask then goes out as numbered text, and
    ``_text_answer`` is what makes that form answerable.
    """
    from robothor.engine.delivery import get_platform_sender

    sender = get_platform_sender("telegram")
    return getattr(getattr(sender, "__self__", None), "bot", None)


async def send_question(chat_id: str, question: str, ask_id: str, options: Sequence[str]) -> str:
    """Put the question on the wire. Returns the delivery form, or ``""``.

    ``""`` is load-bearing: the caller refuses to block on a question that was
    never delivered, because an agent waiting ten minutes for a message nobody
    received is worse than one told immediately that it is on its own.
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
            return ""
        if sent is None:
            return ""
        note_ask_message_id(ask_id, getattr(sent, "message_id", None))
        return "keyboard"

    from robothor.engine.delivery import get_platform_sender

    sender = get_platform_sender("telegram")
    if sender is None:
        logger.warning("Telegram sender not initialized; nobody can be asked")
        return ""

    body = question
    if options:
        # No raw bot to attach a keyboard to, so the options go in the text —
        # and `_text_answer` accepts "1".."N" or the option itself back.
        body += "\n\n" + "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    try:
        sent = await sender(chat_id, body)
    except Exception:  # noqa: BLE001
        logger.exception("Telegram ask %s could not be sent", ask_id)
        return ""
    # The length of the returned list is the only evidence anybody saw it —
    # see the rule in ``channels/base.py``.
    acknowledged, platform_ids = acknowledged_messages(sent)
    if acknowledged == 0:
        return ""
    if platform_ids:
        note_ask_message_id(ask_id, platform_ids[0])
    return "text"


# ─── Inbound handlers ───────────────────────────────────────────────
#
# The bodies live here rather than in ``engine/telegram_handlers.py`` because
# that module is at its size ratchet and because the policy above is what they
# are: two thin adapters from an aiogram update to the binding rules.


def _gate(bot: Any, chat_id: str, sender_id: str) -> tuple[bool, bool]:
    """``(owner_ok, operator_chat)`` for one inbound update."""
    owner_ok = bool(
        bot._check_owner_gate(chat_id=str(chat_id), sender_id=str(sender_id), site="ask_answer")
    )
    operator_chat = str(chat_id) == str(getattr(bot.config, "default_chat_id", ""))
    return owner_ok, operator_chat


async def handle_ask_callback(bot: Any, callback: Any) -> None:
    """Resolve a pending ask from an inline-keyboard tap."""
    msg = callback.message
    if not msg or not hasattr(msg, "chat"):
        await callback.answer(_REFUSED, show_alert=True)
        return

    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Invalid callback data")
        return

    chat_id = str(msg.chat.id)
    sender_id = str(callback.from_user.id) if callback.from_user else ""
    owner_ok, operator_chat = _gate(bot, chat_id, sender_id)

    chosen = resolve_ask_choice(
        parts[1],
        int(parts[2]),
        chat_id=chat_id,
        sender_id=sender_id,
        owner_ok=owner_ok,
        operator_chat=operator_chat,
    )
    if chosen is None:
        # One sentence for "not yours", "already answered" and "the tool gave
        # up". A forger learns nothing; the operator learns the tap did nothing.
        await callback.answer(STALE_ASK)
        return

    await callback.answer(chosen)
    with contextlib.suppress(Exception):
        if hasattr(msg, "edit_reply_markup"):
            await msg.edit_reply_markup(reply_markup=None)


async def intercept_ask_answer(
    bot: Any, chat_id: str, sender_id: str, text: str, reply_to_message_id: str = ""
) -> bool:
    """Whether this message was the answer to a pending ask.

    True means the message was consumed and must NOT reach the run.
    ``_enqueue_message`` buffers a message and returns while a run is active,
    and the buffer is only drained in that run's ``finally`` — so an answer that
    gets that far is invisible to the coroutine blocked inside ``Channel.ask``:
    the ask times out and the reply arrives as the *next* turn's prompt.
    """
    owner_ok, operator_chat = _gate(bot, chat_id, sender_id)
    return resolve_ask_text(
        str(chat_id),
        str(sender_id),
        text,
        owner_ok=owner_ok,
        operator_chat=operator_chat,
        reply_to_message_id=str(reply_to_message_id or ""),
    )
