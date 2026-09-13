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

#: Which channel answers for which trigger. ``webchat`` is absent on purpose:
#: there is no webchat channel, and declaring the name before the path exists
#: is how a registry starts promising surfaces that do not work. A webchat run
#: gets the durable row and the ``approval_required`` status event instead, and
#: the Helm answers it through the bridge.
_CHANNEL_FOR_TRIGGER = {"telegram": "telegram", "slack": "slack"}

#: ``trigger_detail`` prefixes that carry a Telegram chat id. Every interactive
#: Telegram entry point writes one of these; see ``engine/telegram.py`` and
#: ``engine/telegram_plan_mode.py``.
_CHAT_DETAIL_PREFIXES = frozenset({"chat", "plan", "plan-exec", "plan-revise"})


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
    target = _telegram_target(trigger_detail) if channel_name == "telegram" else ""
    timeout = bounded_timeout(args.get("timeout_seconds"))

    from robothor.engine.channels import get_channel

    channel = get_channel(channel_name) if channel_name else None
    if channel is None:
        channel_name = ""

    # The row FIRST, always — before anything that can fail, hang, or be
    # killed. This is the difference between this tool and the in-RAM
    # escalation it sits beside.
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

    answer = await _ask_channel(channel, question, options, timeout, target)
    if answer is None:
        return _unanswered(asked.id, timeout)

    await asyncio.to_thread(
        agent_questions.answer_question,
        asked.id,
        answer,
        answered_by=f"channel:{channel_name}",
        tenant_id=tenant_id,
    )
    return {"answered": True, "answer": answer, "question_id": asked.id}


async def _ask_channel(
    channel: Any, question: str, options: list[str], timeout: float, target: str
) -> str | None:
    """Put the question to ``channel``. ``None`` for every way of not knowing.

    ``NotImplementedError`` is a documented outcome of ``Channel.ask``, not a
    bug — a sink has nobody to ask — and any other exception is a surface that
    broke. Both mean the same thing to the agent: no answer. The distinction is
    already recorded in the row's ``channel`` column.
    """
    if channel is None:
        return None
    try:
        answer = await channel.ask(question, options, timeout=timeout, target=target)
    except NotImplementedError:
        logger.info(
            "Channel %s cannot ask; the question stands as a row", getattr(channel, "name", "?")
        )
        return None
    except Exception:  # noqa: BLE001 — a broken surface is an unanswered question
        logger.exception("Channel ask failed; the question stands as a row")
        return None
    return str(answer) if answer is not None else None


def _unanswered(question_id: str, timeout: float) -> dict[str, Any]:
    """What the agent is told when nobody answered.

    Names the row id on purpose: the question is still open, a late answer is
    still usable, and an agent that knows the id can say so to the operator
    instead of asking the same thing again on its next turn.
    """
    return {
        "answered": False,
        "question_id": question_id,
        "message": (
            f"No answer within {int(timeout)}s. The question is recorded as {question_id} "
            "and can still be answered — proceed with your best judgement and say what you "
            "assumed, or stop and report that you are waiting."
        ),
    }


HANDLERS["ask_user"] = _handle_ask_user
