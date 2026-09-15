"""Asking somebody a question in Teams, and refusing an answer from anyone else.

The pattern is :mod:`robothor.engine.channels.telegram_ask`'s, and the reason it
is copied rather than reinvented is written there: an ask is bound to *where* and
to *who*, and an answer settles it only when both match. An answer typed by one
person must never settle a question asked of another — on Telegram that would be
somebody else's approval; in a Teams channel, where every member can see the card
and press its buttons, it is not even an unusual thing to happen.

So a pending ask here carries the conversation id and the addressee's directory
object id, and :func:`settle_from_activity` checks both before it looks at the
answer. An answer from the wrong person is refused and counted, and the question
goes on waiting for the person it was asked of.

The card
--------
An Adaptive Card. With options, one ``Action.Submit`` per option carrying the
option's INDEX rather than its text — the submit payload comes back from the
client and a button's label is not evidence of anything. Without options, a text
input and one submit.

``None`` is the only non-answer: a timeout, a card that never went out, or an
ask discarded. It is never one of the options, because an option returned
because the clock ran out is an approval nobody gave.

Every value out of a card payload that reaches a log line goes through
``sanitize_log`` first. The payload is composed by whoever pressed the button,
and an ask id carrying a newline writes a second log line of the sender's
choosing — which is how the timeline of an incident ends up part-written by the
person being investigated.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from robothor.identity.scope import PRIVILEGED_ROLES
from robothor.sanitize import sanitize_log

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ASK_OPTIONS",
    "is_ask_submit",
    "ask_over_card",
    "build_card",
    "pending_ask_ids",
    "reset_pending_asks",
    "settle_from_activity",
]

#: The most buttons a card gets. Beyond this an Adaptive Card wraps its actions
#: into an overflow menu, which on some clients is not obviously a menu at all.
MAX_ASK_OPTIONS = 6

#: The key the submit payload carries. Namespaced, because a card in a
#: conversation is not the only thing that can post a ``value``.
ASK_FIELD = "genus_ask"
CHOICE_FIELD = "genus_choice"
TEXT_FIELD = "genus_answer"


@dataclass
class _PendingAsk:
    """One question waiting on one person, in one conversation."""

    future: asyncio.Future[str | None]
    conversation_id: str
    addressee: str
    options: tuple[str, ...] = ()
    delivered: bool = field(default=False)


#: ask id → the question waiting on it. Module level, like Telegram's: the object
#: that asks and the object that hears the answer are reached by different paths
#: (a tool call, an HTTP request), and two dicts would be an ask nobody can ever
#: settle.
_pending: dict[str, _PendingAsk] = {}


def pending_ask_ids() -> list[str]:
    """The ids of questions currently waiting. Introspection, not control."""
    return list(_pending)


def reset_pending_asks() -> None:
    """Drop every pending ask. For tests and for a reload."""
    for pending in list(_pending.values()):
        if not pending.future.done():
            pending.future.cancel()
    _pending.clear()


def build_card(question: str, options: Sequence[str], ask_id: str) -> dict[str, Any]:
    """The Adaptive Card for one question.

    The index travels, not the label: what comes back is a payload the client
    composed, and matching on text would let a relabelled button answer with
    something that was never offered.
    """
    body: list[dict[str, Any]] = [{"type": "TextBlock", "text": question, "wrap": True}]
    actions: list[dict[str, Any]] = []
    if options:
        for index, option in enumerate(options):
            actions.append(
                {
                    "type": "Action.Submit",
                    "title": option,
                    "data": {ASK_FIELD: ask_id, CHOICE_FIELD: index},
                }
            )
    else:
        body.append({"type": "Input.Text", "id": TEXT_FIELD, "isMultiline": True})
        actions.append({"type": "Action.Submit", "title": "Answer", "data": {ASK_FIELD: ask_id}})
    return {
        "type": "AdaptiveCard",
        "version": "1.4",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": body,
        "actions": actions,
    }


async def ask_over_card(
    channel: Any,
    question: str,
    options: Sequence[str] = (),
    *,
    timeout: float = 300.0,
    target: str = "",
    addressee: str = "",
) -> str | None:
    """Put ``question`` to ``addressee`` in ``target``'s conversation.

    Returns the answer, or ``None`` — nobody answered, there was nowhere to ask,
    or the card never went out.
    """
    conversation = str(target or "")
    if not conversation:
        # Guessing a conversation here would ask the wrong people.
        logger.warning("Teams ask has no target conversation; nobody can be asked")
        return None

    opts = tuple(str(option) for option in (options or ()))[:MAX_ASK_OPTIONS]
    reference = await channel.reference_for(conversation)
    if reference is None:
        logger.warning("Teams ask has no recorded conversation for its target")
        return None

    ask_id = uuid.uuid4().hex
    future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    _pending[ask_id] = _PendingAsk(
        future=future,
        # The reference's conversation is what an answer will arrive carrying,
        # which is not necessarily the string the caller passed as `target`
        # (that may have been the person's directory id).
        conversation_id=reference.conversation_id,
        addressee=str(addressee or ""),
        options=opts,
    )
    try:
        delivered = await channel.send_card(conversation, build_card(question, opts, ask_id))
        if not delivered:
            logger.warning("Teams ask %s was never delivered; not waiting", ask_id)
            return None
        _pending[ask_id].delivered = True
        return await asyncio.wait_for(future, timeout=max(1.0, float(timeout)))
    except TimeoutError:
        logger.info("Teams ask %s went unanswered", ask_id)
        return None
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a failed ask is an unanswered one
        logger.exception("Teams ask %s failed", ask_id)
        return None
    finally:
        _pending.pop(ask_id, None)


def _answer_from(pending: _PendingAsk, payload: dict[str, Any]) -> str | None:
    """What this submit payload means for this ask, or ``None``."""
    if pending.options:
        raw = payload.get(CHOICE_FIELD)
        try:
            index = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if 0 <= index < len(pending.options):
            return pending.options[index]
        return None
    text = str(payload.get(TEXT_FIELD) or "").strip()
    return text or None


def is_ask_submit(activity: dict[str, Any]) -> bool:
    """Whether this activity carries one of our card payloads.

    Cheap, and deliberately separate from settling: the endpoint has to decide
    on the request path whether this is a card answer or a message, and the
    decision about *who may answer it* costs an identity lookup that belongs
    after the acknowledgement.
    """
    payload = activity.get("value")
    return isinstance(payload, dict) and bool(str(payload.get(ASK_FIELD) or ""))


def _may_settle(pending: _PendingAsk, native_id: str, identity: Any) -> bool:
    """Whether this sender may settle this ask.

    Two rules, and the second is the one Teams got wrong.

    **A bound addressee is the stronger claim.** The person who was asked
    answers, whatever role they hold — and an owner who was *not* asked does not
    get to answer for them.

    **An empty addressee is not "anybody".** ``channels/base.py`` states it: an
    empty addressee means "whoever the platform's own authorization says may
    answer here", which for Telegram is the operator gate. Teams read it as no
    check at all, and a card in a Teams channel is visible to every member of
    that channel — so an escalation raised for the operator could be settled by
    whoever pressed the button first. The fallback is the same authorization the
    rest of the platform uses: a verified identity holding a privileged role.
    """
    if pending.addressee:
        return pending.addressee == native_id
    if identity is None or not getattr(identity, "verified", False):
        return False
    return str(getattr(identity, "role", "")) in PRIVILEGED_ROLES


def settle_from_activity(
    activity: dict[str, Any],
    *,
    conversation_id: str,
    native_id: str,
    identity: Any = None,
) -> bool:
    """Settle a pending ask from an inbound activity, if it answers one.

    Returns True when this activity WAS an answer — settled or refused — so the
    caller does not also hand it to the agent as a message. A card submit is not
    a sentence somebody typed, and running it as one would drive an agent with
    ``{"genus_ask": "…"}``.

    ``identity`` is the sender's resolved Genus identity, which the caller has
    because the access gate ran first. It is consulted only for an ask with no
    addressee; see :func:`_may_settle`.
    """
    if not is_ask_submit(activity):
        return False
    payload = activity["value"]
    ask_id = str(payload.get(ASK_FIELD) or "")

    pending = _pending.get(ask_id)
    if pending is None or pending.future.done():
        # Usually an ordinary race — a button pressed after the tool gave up, or
        # twice — and sometimes somebody trying ids. Recorded at debug either
        # way, because "the card did nothing" is otherwise unexplainable.
        logger.debug(
            "Teams: a card answer arrived for ask %s, which is not open", sanitize_log(ask_id)
        )
        return True

    if pending.conversation_id != conversation_id or not _may_settle(pending, native_id, identity):
        # The refusal that matters. Counted, and named without naming anybody:
        # a card in a Teams channel is visible to everyone in it, so this is an
        # expected event and not necessarily an attack.
        logger.warning(
            "Teams: refused an answer to ask %s from somebody it was not asked of",
            sanitize_log(ask_id),
        )
        return True

    answer = _answer_from(pending, payload)
    if answer is None:
        return True
    with contextlib.suppress(asyncio.InvalidStateError):
        pending.future.set_result(answer)
    return True
