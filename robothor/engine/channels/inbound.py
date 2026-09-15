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

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT
from robothor.engine.channels import access
from robothor.engine.chat import get_shared_session
from robothor.engine.models import TriggerType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from robothor.identity import IdentityContext

logger = logging.getLogger(__name__)

__all__ = [
    "FAILED_REPLY",
    "NO_OUTPUT_REPLY",
    "InboundResult",
    "handle_message",
    "run_as",
]

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
    display_name: str = "",
    surface: str = access.DIRECT_SURFACE,
    allowlist: Callable[[], bool] | None = None,
    mode: str | None = None,
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
        surface: ``direct`` or ``group`` — see :mod:`robothor.engine.channels.
            access`. A pairing code is only ever minted on a direct one.
        allowlist: the channel's own membership test, called only in
            ``allowlist`` mode.
        mode: the mode the channel resolved for itself, when it has a
            compatibility clause the gate cannot know about (Slack does). Left
            ``None``, the gate reads it.

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
        return InboundResult(reply=decision.refusal, ran=False)

    identity = decision.identity
    resolved_tenant = identity.tenant_id if identity else tenant_id
    session = get_shared_session(session_key)

    try:
        run = await runner.execute(
            agent_id=agent_id,
            message=text,
            trigger_type=trigger_type,
            tenant_id=resolved_tenant,
            user_id=run_as(identity, channel, native_id),
            user_role=(identity.role if identity else "") or "user",
            identity=identity,
            conversation_history=list(session.history) if session.history else None,
        )
    except Exception:
        # Logged with a traceback here, where it reaches the journal; the
        # SENDER gets FAILED_REPLY, which names nothing.
        logger.exception("channel %s could not run an inbound message", channel)
        return InboundResult(reply=FAILED_REPLY, ran=False, failed=True, identity=identity)

    output = str(getattr(run, "output_text", "") or "")
    return InboundResult(
        reply=output or NO_OUTPUT_REPLY,
        ran=True,
        identity=identity,
        run=run,
    )
