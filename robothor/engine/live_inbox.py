"""Messages a conversation sends while its run is still working.

A chat channel creates one ``LiveInbox`` per run and hands it to
``AgentRunner.execute(live_inbox=...)``. Anything the operator sends while that
run is going is ``push``ed here instead of waiting for the run to finish, and
the run drains it at its safe points:

* the top of every iteration, after the previous tool turn's results are all
  recorded. A user turn between an assistant's ``tool_calls`` and their
  results is a provider error, and nothing touches ``session.messages`` inside
  ``run_tool_turn``;
* the moment the model stops calling tools. Without this, a message sent during
  the final LLM call would miss the run by a few seconds and become a second
  turn, which is the "answers twice" behaviour this module exists to remove.

The channel owns the lifecycle. After ``execute`` returns it calls ``close()``;
whatever the run did not pick up comes back and goes out as an ordinary
follow-up turn, so a message is never dropped by the race.

Everything happens on one event loop, so there are no locks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

# Compaction keeps a message carrying this ``_pin``; the key never reaches a
# provider (``strip_engine_keys``).
from robothor.engine.compaction import FOLLOWUP_PIN

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

logger = logging.getLogger(__name__)


#: How many times one run may be extended past a final answer. A conversation
#: that keeps talking must not hold a run open forever; past the cap the rest
#: goes out as a normal follow-up turn.
MAX_LATE_EXTENSIONS = 3

#: Engine key on an assistant turn the operator already received as an interim
#: message. ``get_final_text`` skips it; ``strip_engine_keys`` drops it.
SUPERSEDED_KEY = "_superseded"

_FRAMING = (
    "[Follow-up from the user, sent while you were working. Take it into "
    "account and continue; do not redo work that is already done.]"
)


@dataclass
class FollowUp:
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


class LiveInbox:
    """An ordered mailbox that refuses new mail once closed.

    ``on_interim`` receives an answer the run superseded because follow-ups
    arrived while it was being written. A channel that shows it lets the
    operator see both answers, as in Claude Code or Codex; without one the
    superseded answer stays only in the run's own messages.
    """

    def __init__(self, on_interim: Callable[[str], Awaitable[None]] | None = None) -> None:
        self._items: list[FollowUp] = []
        self._open = True
        self.on_interim = on_interim
        #: Everything the run absorbed, in order. The channel folds it into the
        #: user row it stores, so the next turn knows what the agent was told.
        self.taken: list[FollowUp] = []

    def __len__(self) -> int:
        return len(self._items)

    @property
    def is_open(self) -> bool:
        return self._open

    def push(self, text: str, meta: dict[str, Any] | None = None) -> bool:
        """Queue for the live run. False means the caller must deliver it itself."""
        if not self._open or not (text or "").strip():
            return False
        self._items.append(FollowUp(text, dict(meta or {})))
        return True

    def take(self) -> list[FollowUp]:
        """The run's side: pop everything pending and remember it was taken."""
        items, self._items = self._items, []
        self.taken.extend(items)
        return items

    def close(self) -> list[FollowUp]:
        """The channel's side: stop accepting, return what the run never took."""
        self._open = False
        items, self._items = self._items, []
        return items


def absorb_operator_steer(session: Any) -> None:
    """Operator steers (``/steer``, the runs API, goal updates), at the loop top.

    Here, and only here, the previous tool turn's results are all recorded, so
    a user turn cannot land between an assistant's calls and their results. A
    steer is consumed even when the run then stops, so it cannot survive into a
    resumed run. Chat follow-ups are the opposite: ``check_iteration_guards``
    takes them only once every stop check has passed.
    """
    text = session.consume_pending_steer()
    if text:
        session.messages.append({"role": "user", "content": f"[operator steering update]\n{text}"})
        _count_user_turn(session)
        logger.info("Live steer injected into run %s", session.run_id)


def _count_user_turn(session: Any) -> None:
    """A mid-run message is a user turn too; the memory-review nudge counts it."""
    session._turns_since_memory = getattr(session, "_turns_since_memory", 0) + 1


def absorb_followups(session: Any) -> bool:
    """Append pending follow-ups as one user turn. True when anything landed.

    Nothing is taken during a confirmed routine operation: that turn is
    synthesised without a model (routine_request.py), so a follow-up taken there
    would be recorded as heard by a run that never read it.
    """
    inbox: LiveInbox | None = getattr(session, "live_inbox", None)
    if inbox is None or getattr(session, "routine_operation_bound", False):
        return False
    items = inbox.take()
    if not items:
        return False
    from robothor.engine.chat_backstop import protect_if_human_chat
    from robothor.engine.task_context import record_steering

    # The same payment backstop `session.start` applies to the request: a
    # follow-up is text a person just typed, and it reaches the model the same way.
    texts = [protect_if_human_chat(item.text, session.run.trigger_type) for item in items]
    body = "\n\n".join(texts)
    session.messages.append(
        {"role": "user", "content": f"{_FRAMING}\n{body}", "_pin": FOLLOWUP_PIN}
    )
    for text in texts:
        record_steering(session.messages, text)
    _count_user_turn(session)
    logger.info("Live follow-up (%d message(s)) joined run %s", len(items), session.run_id)
    return True


def absorb_late_followups(session: Any) -> bool:
    """At a final answer: fold in what arrived during the call and keep going."""
    inbox: LiveInbox | None = getattr(session, "live_inbox", None)
    if inbox is None or session.late_extensions >= MAX_LATE_EXTENSIONS:
        return False
    if not absorb_followups(session):
        return False
    session.late_extensions += 1
    return True


async def extend_for_late_followups(session: Any, answer: str | None) -> bool:
    """The loop's final-answer guard: True means ``continue`` instead of ``return``.

    The answer the model just wrote is already in ``session.messages``, so the
    model sees what it said. The channel gets it as an interim message: it was
    a real answer to what the operator had asked at the time.
    """
    if not absorb_late_followups(session):
        return False
    # Already sent as an interim; never report it again as the run's result.
    superseded = next(m for m in reversed(session.messages) if m.get("role") == "assistant")
    superseded[SUPERSEDED_KEY] = True
    await deliver_interim(session, _text_of(answer))
    return True


def _text_of(content: Any) -> str:
    """Plain text, or the text blocks of an extended-thinking block list."""
    if isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(str(b.get("text") or "") for b in blocks)
    return str(content or "")


async def deliver_interim(session: Any, text: str) -> None:
    """Hand the superseded answer to the channel; it must never fail the run."""
    callback = session.live_inbox.on_interim
    if callback is None or not text.strip():
        return
    try:
        await callback(text)
    except Exception as e:  # noqa: BLE001 - delivery is best-effort
        logger.warning("interim delivery failed for run %s: %s", session.run_id, e)


async def keep_going_after_answer(
    session: Any, workspace: str | Path | None, answer: str | None
) -> bool:
    """The model stopped calling tools. True means the loop runs another turn.

    Output repair, then the deliverable nudge, then anything the conversation
    sent during the call (live_inbox.py). The last goes last: an answer the
    engine is about to send back for repair is not one to deliver as interim.
    """
    from robothor.engine.loop_guards import nudge_for_missing_deliverable
    from robothor.engine.output_validation import request_output_repair

    if request_output_repair(session) or nudge_for_missing_deliverable(session, workspace):
        return True
    return await extend_for_late_followups(session, answer)


def history_text(original: str, absorbed: list[FollowUp]) -> str:
    """The user row a channel stores: the request plus what was added mid-run."""
    if not absorbed:
        return original
    added = "\n\n".join(f"[added while working] {item.text}" for item in absorbed)
    return f"{original}\n\n{added}"


def followup_text(session: Any) -> str:
    """What the run took mid-flight, for readers of "what was this task?".

    The deliverable contract reads it: a follow-up can name a deliverable
    exactly as the request can. Only what the run actually took counts; a
    message it never reached goes out as its own turn, with its own contract.
    """
    inbox: LiveInbox | None = getattr(session, "live_inbox", None)
    return "\n\n".join(item.text for item in inbox.taken) if inbox is not None else ""
