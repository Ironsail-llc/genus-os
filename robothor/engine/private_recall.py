"""Who a run may recall private conversation for: the person it is serving.

A conversation segment that belongs to one person (``robothor.memory.segments``)
is verbatim conversation, so it may reach a tool call only when that person is
the one being served, in their own one-to-one conversation, and what the run
produces goes back to them alone. The run's PRINCIPAL is not enough: a
scheduled, hook, workflow or sub-agent run, or a workspace (room) run, can
execute as a member, and its output may be published to a shared audience.

:func:`private_reader_for_run` decides it from what the run itself carries —
its trigger type, its trigger detail (which names the conversation it serves),
its resolved identity — and from the room-run carrier. The run's tool turn
publishes the answer for the turn (:func:`private_reader_scope`, entered by
``tool_turn_context`` around every native tool turn, so both the turn's own
calls and a code snippet's proxied calls see it). Dispatch puts it on
``ToolContext.private_reader``, unless the caller passed one explicitly (as
``tool_call`` does with its caller's); nothing infers it from
``ToolContext.user_id``. Outside a run's tool turn there is none. Anything
this module cannot place is answered with no reader: shared segments only.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from robothor.engine.models import TriggerType

if TYPE_CHECKING:
    from collections.abc import Iterator

    from robothor.identity.context import IdentityContext

#: The trigger-detail prefixes a webchat run writes before its session key
#: (``chat.py``: a turn, and the plan / plan-exec / plan-revise steps).
_WEBCHAT_DETAIL_PREFIXES = ("webchat:", "plan:", "plan-exec:", "plan-revise:")


@dataclass(frozen=True)
class PrivateReader:
    """The person a run serves, as the owner principals of their conversations.

    ``person_id`` matches ``person:{id}`` segments, ``user_id`` matches the
    ``user:{id}`` segments of a member's own webchat conversation.
    """

    person_id: str | None
    user_id: str | None


def _webchat_session_key(detail: str | None) -> str | None:
    for prefix in _WEBCHAT_DETAIL_PREFIXES:
        if detail and detail.startswith(prefix):
            return detail[len(prefix) :] or None
    return None


def _telegram_chat_id(detail: str | None) -> int | None:
    """The chat id in a Telegram run's ``chat:{id}|sender:…`` detail."""
    if not detail or not detail.startswith("chat:"):
        return None
    raw = detail[len("chat:") :].split("|", 1)[0]
    try:
        return int(raw)
    except ValueError:
        return None


def _main_session_key() -> str:
    from robothor.memory.segments import _main_session_key

    return _main_session_key()


def private_reader_for_run(
    trigger_type: Any,
    trigger_detail: str | None,
    user_id: str | None,
    identity: IdentityContext | None,
) -> PrivateReader | None:
    """The person this run serves in a one-to-one conversation, or None.

    A reader exists only when all of these hold:

    - the run is not inside a workspace (no room-run carrier): a workspace's
      output reaches every member;
    - it has a verified identity — the person a human-typed turn came from;
    - it is a webchat turn in that person's own conversation — their
      ``agent:{agent}:user:{user_id}`` session, or the main session when the
      identity is the operator's — or a Telegram direct message (a positive
      chat id; groups are negative).

    Every other run — cron, hook, event, workflow, sub-agent, federation,
    channel events, manual and IDE runs, Slack and plugin channels, whose
    audience the run cannot tell — has no reader.
    """
    from robothor.rooms.run_context import current_room_run

    if current_room_run() is not None:
        return None
    if identity is None or not identity.verified:
        return None
    person = identity.person_id or None

    if trigger_type == TriggerType.WEBCHAT:
        key = _webchat_session_key(trigger_detail)
        if key is None or not user_id:
            return None
        parts = key.split(":")
        own = len(parts) == 4 and parts[0] == "agent" and parts[2] == "user"
        if own and parts[3] == user_id:
            return PrivateReader(person, user_id)
        if key == _main_session_key() and identity.role == "owner":
            return PrivateReader(person, user_id)
        return None

    if trigger_type == TriggerType.TELEGRAM:
        chat_id = _telegram_chat_id(trigger_detail)
        if chat_id is not None and chat_id > 0 and person:
            return PrivateReader(person, None)
        return None

    return None


def private_reader_for_session(session: Any) -> PrivateReader | None:
    """:func:`private_reader_for_run` for a running ``AgentSession``."""
    run = getattr(session, "run", None)
    if run is None:
        return None
    return private_reader_for_run(
        trigger_type=getattr(run, "trigger_type", None),
        trigger_detail=getattr(run, "trigger_detail", None),
        user_id=getattr(run, "user_id", None),
        identity=getattr(session, "identity", None),
    )


#: The private reader of the tool turn running in this context, if any.
_turn_reader: ContextVar[PrivateReader | None] = ContextVar("private_reader", default=None)


@contextmanager
def private_reader_scope(session: Any) -> Iterator[PrivateReader | None]:
    """Publish ``session``'s private reader for one tool turn.

    Decided on entry, from the run and the room-run carrier current then; a
    run without one publishes ``None`` explicitly, so a sub-agent spawned
    inside a parent's turn never inherits the parent's reader.
    """
    token = _turn_reader.set(private_reader_for_session(session))
    try:
        yield _turn_reader.get()
    finally:
        _turn_reader.reset(token)


def current_private_reader() -> PrivateReader | None:
    """The private reader of the current tool turn; ``None`` outside one."""
    return _turn_reader.get()
