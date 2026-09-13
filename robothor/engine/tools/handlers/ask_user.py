"""`ask_user` — the tool an agent calls instead of guessing.

Everything an agent does that a person would have said "no, the other one" to
is a defect this tool exists to prevent. But the tool is only worth having if
it is honest about three things, and all three are failures this platform has
shipped before:

1. **It must not invent an answer.** A timeout returns ``answered: False``, not
   the first option. ``Channel.ask`` returns ``None`` for "nobody answered" and
   this handler never turns that into a choice.
2. **It must not ask where nobody is listening.** A cron run has no person on
   the other end. Asking anyway would suspend a scheduled agent for ten minutes
   and then report a timeout as if the operator had been rude. The refusal is a
   sentence the model can act on ("nobody is on the other end of this run") —
   which is why the gate lives here and not in ``ToolRegistry``, where the tool
   would simply be absent and the model would have no idea why.
3. **The question must outlive the wait.** The row goes into
   ``agent_questions`` BEFORE the channel is called, so an answer that arrives
   after the tool gave up is still an answer somebody can use, and a question
   nobody ever saw is still visible to the operator.

Timeout arithmetic is load-bearing. ``ToolRegistry.execute`` wraps this call in
``asyncio.timeout``, so an ask that waits longer than its own tool budget is a
guaranteed ``TimeoutError`` rather than a question. Two things keep that from
happening, and both are needed: ``ask_user`` is in ``_LONG_RUNNING_TOOLS`` (so
the budget is at least ``ASK_TOOL_BUDGET``) and :func:`bounded_timeout` caps the
wait strictly below it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from robothor.engine import agent_questions, run_status, tracking

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

#: The wall-clock budget ``runner._resolve_tool_timeout`` gives this tool.
#: Duplicated as a constant rather than imported to keep a tool handler from
#: importing the runner; the test asserts the two agree.
ASK_TOOL_BUDGET = 600

#: How far below the tool budget the wait is capped. Equal would race the
#: registry's own ``asyncio.timeout`` and turn "nobody answered" — a fact worth
#: reporting — into a tool failure, which is not.
ASK_TIMEOUT_HEADROOM = 10

#: The ceiling a caller may request. Same number as the budget so the schema
#: and the runner tell the model the same story.
MAX_ASK_TIMEOUT = 600

DEFAULT_ASK_TIMEOUT = 300.0


#: Telegram renders more than a handful of buttons as a wall. Mirrors
#: ``channels.telegram.MAX_ASK_OPTIONS`` so the truncation is announced in the
#: result rather than discovered in the chat.
MAX_OPTIONS = 6

#: Triggers with a person on the other end. Everything else (cron, hooks,
#: events, sub-agents, workflows, federation, webhooks) is a run nobody is
#: watching.
INTERACTIVE_TRIGGERS = frozenset({"telegram", "webchat", "slack", "ide"})

#: Which channel answers for which trigger. ``webchat`` joined with C9: the
#: channel exists now, so a Helm run WAITS — the question goes out over the
#: run's SSE stream, the browser answers the durable row through the bridge, and
#: the channel learns by polling the row. Before that the row and the status
#: event went out and only a later turn could see the answer.
_CHANNEL_FOR_TRIGGER = {"telegram": "telegram", "slack": "slack", "webchat": "webchat"}

#: ``trigger_detail`` prefixes that carry a Telegram chat id. Every interactive
#: Telegram entry point writes one of these; see ``engine/telegram.py`` and
#: ``engine/telegram_plan_mode.py``.
_CHAT_DETAIL_PREFIXES = frozenset({"chat", "plan", "plan-exec", "plan-revise"})

#: The segment of a derived webchat session key that precedes the member's
#: ``user_accounts.id`` (``agent:{agent}:user:{id}``, see
#: ``chat.derive_user_session_key``).
_WEBCHAT_USER_SEGMENT = ":user:"


def bounded_timeout(requested: Any) -> float:
    """The wait this handler will actually do, in seconds.

    Always strictly below ``ASK_TOOL_BUDGET`` and always positive: a caller
    asking for zero, for nothing, or for nonsense gets the default rather than
    an ask that ends before it starts.
    """
    try:
        value = float(requested)
    except (TypeError, ValueError):
        value = DEFAULT_ASK_TIMEOUT
    if value <= 0:
        value = DEFAULT_ASK_TIMEOUT
    return float(min(value, MAX_ASK_TIMEOUT, ASK_TOOL_BUDGET - ASK_TIMEOUT_HEADROOM))


def _telegram_target(trigger_detail: str) -> str:
    """The chat id a Telegram run came from, or "" if the detail does not say."""
    head = (trigger_detail or "").split("|", 1)[0]
    prefix, _, value = head.partition(":")
    return value if prefix in _CHAT_DETAIL_PREFIXES else ""


def _webchat_target(ctx: ToolContext, trigger_detail: str) -> str:
    """The member's ``user_accounts.id`` a webchat ask is aimed at, or "".

    The resolved identity first — it is DB-verified — then the session key in
    the trigger detail, which carries the same id because the derivation put it
    there. The owner's shared key (``agent:main:primary``) names no user, so it
    yields "": inventing one would aim a delivery at somebody who was never
    asked.
    """
    identity = getattr(ctx, "identity", None)
    if identity is not None and getattr(identity, "verified", False):
        account = str(getattr(identity, "user_account_id", "") or "")
        if account:
            return account
    head = (trigger_detail or "").split("|", 1)[0]
    prefix, _, key = head.partition(":")
    if prefix != "webchat" or _WEBCHAT_USER_SEGMENT not in key:
        return ""
    return key.rsplit(_WEBCHAT_USER_SEGMENT, 1)[1]


def _addressee(ctx: ToolContext, trigger: str) -> str:
    """The channel-native id of the person this run is talking to, or "".

    This is what the channel binds the pending ask to, so that the answer which
    settles it has to come from the person who was asked rather than from
    whoever happens to be authorized in some chat. ``IdentityContext.identifier``
    is that id for an interactive run — the Telegram sender id, not the chat.

    Empty for an unverified or absent identity, which is the safe direction: an
    unaddressed ask falls back to the platform's own authorization (for Telegram,
    the owner gate) rather than binding to an identity nobody proved.
    """
    identity = getattr(ctx, "identity", None)
    if identity is None or not getattr(identity, "verified", False):
        return ""
    if str(getattr(identity, "channel", "")) != trigger:
        return ""
    return str(getattr(identity, "identifier", "") or "")


async def _handle_ask_user(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Ask the person who started this run a question, and wait for the answer."""
    question = str(args.get("question") or "").strip()
    if not question:
        return {"error": "question is required"}

    raw_options = args.get("options") or []
    if not isinstance(raw_options, list | tuple):
        return {"error": "options must be a list of strings"}
    options = [str(o).strip() for o in raw_options if str(o).strip()][:MAX_OPTIONS]

    run_id = getattr(ctx, "run_id", "") or ""
    tenant_id = getattr(ctx, "tenant_id", "") or ""
    agent_id = getattr(ctx, "agent_id", "") or ""

    run = await asyncio.to_thread(tracking.get_run, run_id) if run_id else None
    trigger = str((run or {}).get("trigger_type") or "")
    if trigger not in INTERACTIVE_TRIGGERS:
        # No row: a question nobody could ever have been asked is noise in the
        # operator's backlog, not a record.
        return {
            "error": (
                f"ask_user needs a person on the other end of the run, and this one was "
                f"started by '{trigger or 'an unknown trigger'}'. Nobody is watching it, so "
                "the question would go unanswered. Decide with what you have, or file a CRM "
                "task for the operator to pick up."
            )
        }

    trigger_detail = str((run or {}).get("trigger_detail") or "")
    channel_name = _CHANNEL_FOR_TRIGGER.get(trigger, "")
    target = ""
    if channel_name == "telegram":
        target = _telegram_target(trigger_detail)
    elif channel_name == "webchat":
        target = _webchat_target(ctx, trigger_detail)
    addressee = _addressee(ctx, trigger)
    timeout = bounded_timeout(args.get("timeout_seconds"))

    from robothor.engine.channels import get_channel

    channel = get_channel(channel_name) if channel_name else None
    if channel is None:
        channel_name = ""

    # The row FIRST, always — before anything that can fail, hang, or be
    # killed. This is the difference between this tool and the in-RAM
    # escalation it sits beside. No row means no durable question, so there is
    # nothing honest to return but a refusal — and a bare traceback is not one.
    try:
        asked = await asyncio.to_thread(
            agent_questions.ask_question,
            run_id=run_id,
            agent_id=agent_id,
            question=question,
            options=options,
            channel=channel_name,
            target=target,
            timeout_seconds=timeout,
            tenant_id=tenant_id,
        )
    except Exception:  # noqa: BLE001 — a store that is down is not a crash here
        logger.exception("ask_user could not record the question; not asking")
        return {
            "error": (
                "the question could not be recorded, so it was not asked — nothing would "
                "have been able to carry an answer back. Decide with what you have."
            )
        }

    await run_status.emit_status(
        run_id,
        {
            "event": "approval_required",
            "kind": "question",
            "id": asked.id,
            "run_id": run_id,
            "agent_id": agent_id,
            "question": question,
            "options": options,
            "expires_at": asked.expires_at.isoformat() if asked.expires_at else None,
        },
    )

    answer, delivered, waited = await _ask_channel(
        channel, question, options, timeout, target, addressee, asked.id, run_id
    )
    if answer is None:
        return _unanswered(asked.id, waited, delivered=delivered)

    try:
        await asyncio.to_thread(
            agent_questions.answer_question,
            asked.id,
            answer,
            answered_by=f"channel:{channel_name}",
            tenant_id=tenant_id,
        )
    except Exception:  # noqa: BLE001
        # The person answered. Losing that because the settle write failed
        # would throw away the only thing this tool exists to obtain; the row
        # stays pending and the watchdog will expire it, which is a worse record
        # than the truth but a far better outcome than a discarded answer.
        logger.exception("ask_user could not settle question %s; returning the answer", asked.id)
    return {"answered": True, "answer": answer, "question_id": asked.id}


async def _ask_channel(
    channel: Any,
    question: str,
    options: list[str],
    timeout: float,
    target: str,
    addressee: str,
    question_id: str = "",
    run_id: str = "",
) -> tuple[str | None, bool, float]:
    """Put the question to ``channel``. Returns ``(answer, delivered, waited)``.

    Two facts the caller needs and one it must not invent.

    ``delivered`` separates the two very different causes of "no answer": the
    person was asked and stayed silent, or **nobody was ever asked** — there is
    no channel for this trigger, or the channel refuses to ask
    (``NotImplementedError``, a documented outcome and not a bug). Only the
    first is a question anybody could have answered.

    ``waited`` is measured, not assumed. The first cut reported the *requested*
    timeout — "no answer within 300s" — on paths that returned in the same tick,
    which handed the model a fabricated elapsed-time claim on the one tool whose
    whole premise is not fabricating things. A measured number is right on every
    path, including a channel that accepted the call and then could not put the
    question on the wire.
    """
    if channel is None:
        return None, False, 0.0
    # A CAPABILITY PROBE, not an unconditional kwarg. The webchat channel has no
    # inbound socket, so its only return path is the durable row — it declares
    # ``ask_wants_question_id`` and is handed the row id and the run to emit on.
    # Telegram's and Slack's ``ask`` signatures take no ``**kw``, so passing
    # these to them would raise ``TypeError``, be swallowed by the except below
    # as "the channel could not ask", and silently break every Telegram ask.
    extra: dict[str, Any] = (
        {"question_id": question_id, "run_id": run_id}
        if getattr(channel, "ask_wants_question_id", False)
        else {}
    )
    started = time.monotonic()
    try:
        answer = await channel.ask(
            question, options, timeout=timeout, target=target, addressee=addressee, **extra
        )
    except NotImplementedError:
        logger.info(
            "Channel %s cannot ask; the question stands as a row", getattr(channel, "name", "?")
        )
        return None, False, time.monotonic() - started
    except Exception:  # noqa: BLE001 — a broken surface is an unanswered question
        logger.exception("Channel ask failed; the question stands as a row")
        return None, False, time.monotonic() - started
    waited = time.monotonic() - started
    return (str(answer), True, waited) if answer is not None else (None, True, waited)


def _unanswered(question_id: str, waited: float, *, delivered: bool) -> dict[str, Any]:
    """What the agent is told when no answer came back.

    Names the row id on purpose: the question is still open, a late answer is
    still usable, and an agent that knows the id can say so to the operator
    instead of asking the same thing again on its next turn.

    ``delivered`` picks between two honest sentences, and ``waited`` is the
    measured elapsed time rather than the budget that was requested. Neither
    sentence claims a wait that did not happen, and the un-delivered one says
    plainly that the answer, if it comes, reaches a *later* turn — for a surface
    with no in-run reply path (webchat today) the Helm answers through the
    bridge endpoint long after this run has finished.
    """
    if not delivered:
        return {
            "answered": False,
            "delivered": False,
            "question_id": question_id,
            "message": (
                "no channel could deliver this question; it is recorded as "
                f"{question_id} and can be answered from the Helm or CLI, which a later "
                "turn will see. Decide with what you have and say what you assumed."
            ),
        }
    return {
        "answered": False,
        "delivered": True,
        "question_id": question_id,
        "message": (
            f"No answer after {int(waited)}s. The question is recorded as {question_id} "
            "and can still be answered — proceed with your best judgement and say what you "
            "assumed, or stop and report that you are waiting."
        ),
    }


HANDLERS["ask_user"] = _handle_ask_user
