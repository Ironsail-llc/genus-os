"""What happens between a message arriving and a reply going out. Once.

``engine/telegram.py`` and ``engine/slack.py`` each carried the whole of it:
gate → identity → session → ``runner.execute`` → reply. :mod:`robothor.engine.
channels.base` says plainly that unifying those "needs a second real inbound
surface to generalise against", and ``genus-teams`` is it — a channel that
receives, shipped as a *plugin*, which cannot reach into the engine to copy a
method even if somebody wanted to.

Copying is the failure this prevents. Every line of that sequence is a decision:
which access mode is in force, which surface a pairing code may be minted on,
who the run is attributed to when nobody is bound, what an empty output says,
and — the one that has actually gone wrong on this platform — whether a failure
reaches the sender as an apology or as a 500 the platform then retries into the
same failure. Two copies means two places for one of those to be wrong, each
with its own passing tests.

What stays with the channel
---------------------------
How a message arrives, how a reply goes back (chunking, a card, a ``say``
callback), and the two facts only it can know: the ``surface`` (a DM or a room)
and, where it has a compatibility clause of its own, the ``mode``. Everything
else is here.

This function never raises. A channel handler that propagated would answer a
webhook with a 500, and Teams — like every platform with an at-least-once
delivery window — would send the same activity again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT
from robothor.engine import live_control
from robothor.engine.channels import access
from robothor.engine.chat import get_shared_session
from robothor.engine.chat_history import append_turn
from robothor.engine.models import RunStatus, TriggerType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Awaitable, Callable

    from robothor.identity import IdentityContext

logger = logging.getLogger(__name__)

__all__ = [
    "CHANNEL_DETAIL_PREFIX",
    "FAILED_REPLY",
    "NO_OUTPUT_REPLY",
    "InboundResult",
    "conversation_turn",
    "handle_message",
    "run_as",
]

#: The trigger-detail namespace a plugin channel's run is recorded under:
#: ``channel:<channel name>:<reply target>``. ``ask_user`` parses it back, which
#: is what lets a channel the platform has never heard of be asked a question.
#: Spelled here rather than in the tool because THIS is the writer.
CHANNEL_DETAIL_PREFIX = "channel"

#: What a run with no output text says. Not silence: a sender who typed
#: something and got nothing back cannot tell "it worked and said nothing" from
#: "it is broken", and the second is what they will assume.
NO_OUTPUT_REPLY = "I processed your request but have no output to share."

#: What a failed run says. Deliberately carries **nothing** of the exception:
#: the text goes to whoever sent the message, who may be a stranger three
#: seconds into a pairing attempt, and a psycopg2 error names the database host
#: and the account it tried.
FAILED_REPLY = "Something went wrong. Please try again."


@dataclass(frozen=True)
class InboundResult:
    """What the channel should do now.

    ``reply`` is the exact text to send back, and ``""`` means **send
    nothing** — a suppressed pairing reply and a silently-ignored group message
    are both legitimate, and a channel that invented a message for them would
    be telling a stranger that somebody is listening.

    ``ran`` and ``failed`` are separate because "refused" and "ran and broke"
    are different facts and a caller may want to count them apart. Neither is
    derivable from ``reply``.
    """

    reply: str = ""
    ran: bool = False
    failed: bool = False
    identity: IdentityContext | None = None
    run: Any | None = None
    #: WHICH of the four outcomes this is: ``refused``, ``answer``,
    #: ``no_output`` or ``failed``. Named rather than inferred, because a
    #: channel that reconstructed it by comparing ``reply`` against the two
    #: sentence constants would answer differently the day an agent's output
    #: happens to be one of them — and Slack renders the four shapes
    #: differently (a chunked mrkdwn post, a plain sentence, a bare refusal).
    kind: str = "refused"


#: One lock per conversation, held across its run, so a visitor's second
#: message reads the history their first one wrote instead of racing it. Keyed
#: by ``session_key``; an entry lives only while a message for it is running or
#: waiting, because a public channel's visitors are unbounded and a lock kept
#: per visitor forever is a leak.
_session_locks: dict[str, asyncio.Lock] = {}
_session_waiters: dict[str, int] = {}

#: The fleet-wide cap: ``(limit, loop, semaphore)``. Rebuilt when the setting
#: changes, so a new value applies without a restart; runs already holding a
#: slot of the old one finish on it.
_fleet: tuple[int, asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


@contextlib.asynccontextmanager
async def conversation_turn(session_key: str) -> AsyncIterator[None]:
    """Hold ``session_key``'s turn; different keys never wait on each other.

    Public so a channel's own commands that change a conversation's history
    (Slack's ``/clear``) wait for a run in flight rather than racing its
    write-back.
    """
    lock = _session_locks.setdefault(session_key, asyncio.Lock())
    _session_waiters[session_key] = _session_waiters.get(session_key, 0) + 1
    try:
        async with lock:
            yield
    finally:
        _session_waiters[session_key] -= 1
        if not _session_waiters[session_key]:
            del _session_waiters[session_key]
            _session_locks.pop(session_key, None)


def _fleet_slots() -> asyncio.Semaphore:
    """The semaphore every channel run takes a slot of. Read lazily, each time."""
    global _fleet
    from robothor.settings import get_settings

    limit = max(1, int(get_settings().channels.inbound_max_concurrent_runs))
    loop = asyncio.get_running_loop()
    if _fleet is None or _fleet[0] != limit or _fleet[1] is not loop:
        _fleet = (limit, loop, asyncio.Semaphore(limit))
    return _fleet[2]


def _last_turns(history: list[Any], turns: int) -> list[Any]:
    """The rows of the last ``turns`` exchanges, each starting at a user row."""
    if turns <= 0:
        return []
    starts = [
        i for i, row in enumerate(history) if isinstance(row, dict) and row.get("role") == "user"
    ]
    if len(starts) <= turns:
        return list(history)
    return list(history[starts[-turns] :])


def run_as(identity: IdentityContext | None, channel: str, native_id: str) -> str:
    """The ``user_id`` an inbound run is attributed to.

    A bound identity's own id, preferring the ``tenant_users.user_id`` the
    permission tables are keyed on. With nothing bound — only ``open`` and
    ``allowlist`` mode reach here — a synthetic ``<channel>:<native id>``, named
    explicitly rather than left to look like a real account: an authorization
    decision about a string that matches no row anywhere is a bug this platform
    has already shipped once, on Slack.
    """
    if identity is None:
        return f"{channel}:{native_id}"
    return str(identity.tenant_user_id or identity.user_account_id or f"{channel}:{native_id}")


async def handle_message(
    *,
    channel: str,
    native_id: str,
    text: str,
    runner: Any,
    tenant_id: str = DEFAULT_TENANT,
    session_key: str,
    trigger_type: TriggerType = TriggerType.CHANNEL,
    agent_id: str = "main",
    reply_target: str = "",
    display_name: str = "",
    surface: str = access.DIRECT_SURFACE,
    allowlist: Callable[[], bool] | None = None,
    mode: str | None = None,
    record_turn: bool = True,
    history_limit: int | None = None,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> InboundResult:
    """Gate one inbound message and, if it is allowed, run it.

    Args:
        channel: the registry name — ``slack``, ``teams``. Decides which access
            mode applies and which identity rows are consulted.
        native_id: the platform's own id for the sender. Never a display name.
        text: what they said, already stripped of whatever the channel wraps it
            in (a bot mention, a command prefix).
        runner: the :class:`~robothor.engine.runner.AgentRunner`.
        session_key: the shared-session key, so a conversation on one surface
            keeps its history. The channel owns its shape.
        reply_target: where an answer to THIS message goes — the conversation it
            arrived in, not the sender. Recorded as the run's trigger detail
            (``channel:<name>:<target>``), which is how ``ask_user`` finds a
            plugin channel and its address later, and how a question asked in a
            shared room is answered in that room rather than wherever the sender
            happens to have spoken most recently.
        surface: ``direct`` or ``group`` — see :mod:`robothor.engine.channels.
            access`. A pairing code is only ever minted on a direct one.
        allowlist: the channel's own membership test, called only in
            ``allowlist`` mode.
        mode: the mode the channel resolved for itself, when it has a
            compatibility clause the gate cannot know about (Slack does). Left
            ``None``, the gate reads it.
        record_turn: append the exchange to the session's history after a
            run that COMPLETED with output and persist it through the chat
            store, so the conversation remembers it across restarts. A refused,
            failed, timed-out or empty run records nothing.
        history_limit: hand the run only the last N turns (user + assistant
            pairs) of the session's history. ``None`` hands it all of it.
        on_progress: optional. Called with the run's tool and status events
            (``tool_start``, ``tool_end``, ``iteration_start`` and the rest), the
            runner's ``on_tool`` and ``on_status``; feed them to
            :class:`robothor.engine.live_control.ProgressMessage`. ``None``, the
            run reports no progress, as before.

    The run executes inside ``live_control.conversation_scope(session_key)``, so
    ``/steer`` and ``/stop`` on the channel can reach it by its session key.

    Messages sharing a ``session_key`` run one after another; different
    conversations run concurrently, up to the fleet-wide
    ``ROBOTHOR_CHANNEL_MAX_CONCURRENT_RUNS`` — beyond it they queue.

    Returns:
        :class:`InboundResult`. Never raises.
    """
    decision = await access.evaluate(
        channel,
        native_id,
        tenant_id=tenant_id,
        display_name=display_name,
        surface=surface,
        allowlist=allowlist,
        mode=mode,
    )
    if not decision.allowed:
        return InboundResult(reply=decision.refusal, ran=False, kind="refused")

    identity = decision.identity
    resolved_tenant = identity.tenant_id if identity else tenant_id

    # `channel:<name>:<target>` — the only place this string is written. A
    # channel-triggered run cannot say which channel it was from the enum (there
    # is one member for every plugin channel), so the detail carries it, and
    # `ask_user` reads the channel and the reply address back out.
    # Written for the CHANNEL trigger only. Slack passes none and keeps passing
    # none: this refactor is required to leave the surface it extracted
    # byte-identical, and a trigger detail is a row a dashboard renders.
    detail = (
        f"{CHANNEL_DETAIL_PREFIX}:{channel}:{reply_target}"
        if reply_target and trigger_type == TriggerType.CHANNEL
        else None
    )

    # The conversation's turn outside, the fleet slot inside: a message queued
    # behind its own conversation must not hold a slot another visitor could use.
    async with conversation_turn(session_key):
        session = get_shared_session(session_key)
        history = list(session.history) if session.history else []
        if history_limit is not None:
            history = _last_turns(history, history_limit)
        try:
            progress: dict[str, Any] = (
                {"on_tool": on_progress, "on_status": on_progress} if on_progress else {}
            )
            async with _fleet_slots():
                with live_control.conversation_scope(session_key):
                    run = await runner.execute(
                        agent_id=agent_id,
                        message=text,
                        trigger_type=trigger_type,
                        trigger_detail=detail,
                        tenant_id=resolved_tenant,
                        user_id=run_as(identity, channel, native_id),
                        user_role=(identity.role if identity else "") or "user",
                        identity=identity,
                        conversation_history=history or None,
                        **progress,
                    )
        except Exception:
            # Logged with a traceback here, where it reaches the journal; the
            # SENDER gets FAILED_REPLY, which names nothing.
            logger.exception("channel %s could not run an inbound message", channel)
            return InboundResult(
                reply=FAILED_REPLY, ran=False, failed=True, identity=identity, kind="failed"
            )

        output = str(getattr(run, "output_text", "") or "")
        # Only a COMPLETED run with something to say is remembered. The runner
        # reports most failures by RETURNING the run (failed, timeout,
        # cancelled), and a timed-out run can carry partial text; recording
        # either would teach the next turn an answer that was never given.
        if record_turn and output and getattr(run, "status", None) == RunStatus.COMPLETED:
            # Guarded: this function never raises, and the answer is already
            # made — a history that could not be written loses memory, not it.
            try:
                append_turn(session, user_message=text, assistant_text=output)
            except Exception:
                logger.exception("channel %s could not record a turn", channel)
            # Persisted the way the Helm's turns are, so the conversation is
            # restored after a restart. Awaited inside the conversation's turn
            # so two exchanges are stored in the order they happened; the call
            # logs its own store failures; the guard is for anything else.
            from robothor.engine.chat_store import save_exchange_async

            try:
                await save_exchange_async(
                    session_key,
                    text,
                    output,
                    channel=channel,
                    model_override=getattr(session, "model_override", None),
                    tenant_id=resolved_tenant,
                )
            except Exception:
                logger.exception("channel %s could not persist a turn", channel)

    return InboundResult(
        reply=output or NO_OUTPUT_REPLY,
        ran=True,
        identity=identity,
        run=run,
        kind="answer" if output else "no_output",
    )
