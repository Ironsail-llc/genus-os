"""Helm web chat: a message sent while the conversation's run is still working.

The web half of live follow-ups (robothor/engine/live_inbox.py; Telegram's is
telegram_live.py). ``/chat/send`` opens a ``LiveInbox`` for every run and parks
it on the ``ChatSession``. A later ``/chat/send`` carrying ``join_running: true``
for the same session, from the same person, goes into that inbox instead of
starting a second run, and gets a one-event stream back:

* ``followup_joined``: the running turn has it and will take it at its next
  safe point.
* ``followup_queued``: nothing running can take it: the session is idle (the
  turn ended while the message was on its way), or busy with something that
  cannot take it (a plan run, another person's turn, a run that has already
  returned). The browser sends it as an ordinary turn once its own stream has
  ended. A flagged request never starts a run itself: the browser's follow-up
  path has no stream renderer, and a reply it cannot show is a lost reply.

The running turn's own stream carries the rest:

* ``interim``: an answer the run superseded because follow-ups arrived while it
  was written. The browser shows it as its own message; streaming restarts.
* ``pending_followups``: what the run never took. The browser sends it as the
  next turn, so the race can delay a message but never drop it.

Joining is opt-in. The unflagged contract is load-bearing: two plain
``/chat/send`` calls on one session both run and both end in ``done``, and API
clients other than the Helm depend on that (test_concurrent_session.py).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.responses import StreamingResponse

from robothor.engine.live_inbox import LiveInbox

FOLLOWUP_JOINED = "followup_joined"
FOLLOWUP_QUEUED = "followup_queued"


def _owner(auth: Any) -> tuple[str, str]:
    return (str(auth.tenant_id), str(auth.user_id))


def open_live_inbox(session: Any, auth: Any) -> LiveInbox:
    """Before the run task exists, so no request can find the session busy
    with no inbox to join."""
    inbox = LiveInbox()
    session.live_inbox, session.live_owner = inbox, _owner(auth)
    return inbox


def join_running_turn(session: Any, auth: Any, message: str) -> str:
    """``followup_joined`` when the running turn took it, else ``followup_queued``."""
    task = session.active_task
    inbox: LiveInbox | None = session.live_inbox
    if task is None or task.done() or inbox is None or session.live_owner != _owner(auth):
        return FOLLOWUP_QUEUED
    return FOLLOWUP_JOINED if inbox.push(message) else FOLLOWUP_QUEUED


def seal_live_inbox(session: Any, inbox: LiveInbox) -> list[str]:
    """Stop accepting; return what the run never took. Idempotent."""
    if session.live_inbox is inbox:
        session.live_inbox = session.live_owner = None
    return [item.text for item in inbox.close()]


def drop_live_inbox(session: Any) -> None:
    """``/chat/abort`` and ``/chat/clear``: what was added goes with the run."""
    inbox: LiveInbox | None = session.live_inbox
    if inbox is not None:
        inbox.close()
    session.live_inbox = session.live_owner = None


def single_event_response(event: str) -> StreamingResponse:
    """The answer to a follow-up: one SSE event. The Helm proxy pipes an engine
    body through as ``text/event-stream`` whatever it is, so it stays SSE."""

    async def body() -> Any:
        yield f"event: {event}\ndata: {json.dumps({'event': event})}\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")
