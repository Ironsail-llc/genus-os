"""Helm web chat: a message sent while the conversation's run is still working.

The web half of live follow-ups (robothor/engine/live_inbox.py; Telegram's is
telegram_live.py). ``/chat/send`` opens a ``LiveInbox`` for every run and parks
it on the ``ChatSession`` under the run's request id: two tabs, two turns, two
inboxes. A later ``/chat/send`` carrying ``join_running: true`` and the
``running_request_id`` of the turn it is for, from the same person, goes into
that turn's inbox instead of starting a second run, and gets one event back:

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
from typing import TYPE_CHECKING, Any

from fastapi.responses import StreamingResponse

from robothor.engine.runtime.chat_control import request_key

if TYPE_CHECKING:
    from robothor.engine.live_inbox import LiveInbox

FOLLOWUP_JOINED = "followup_joined"
FOLLOWUP_QUEUED = "followup_queued"


def _owner(auth: Any) -> tuple[str, str]:
    return (str(auth.tenant_id), str(auth.user_id))


def register_live_inbox(session: Any, auth: Any, inbox: LiveInbox) -> None:
    """Park the run's inbox under its request id.

    Called right after ``chat_control.start`` has set ``active_request_id`` and
    before anything awaits, so the run task has not taken a step yet: no request
    can find the turn running with no inbox to join.
    """
    session.live_inboxes[session.active_request_id] = (inbox, _owner(auth))


def join_running_turn(session: Any, auth: Any, session_key: str, body: dict[str, Any]) -> str:
    """``followup_joined`` when the named turn took it, else ``followup_queued``."""
    running = body.get("running_request_id")
    if not running:
        return FOLLOWUP_QUEUED
    entry = session.live_inboxes.get(request_key(auth, session_key, running))
    if entry is None or entry[1] != _owner(auth):
        return FOLLOWUP_QUEUED
    return FOLLOWUP_JOINED if entry[0].push(str(body.get("message", ""))) else FOLLOWUP_QUEUED


def seal_live_inbox(session: Any, inbox: LiveInbox) -> list[str]:
    """Stop accepting; return what the run never took. Idempotent."""
    for key in [k for k, entry in session.live_inboxes.items() if entry[0] is inbox]:
        del session.live_inboxes[key]
    return [item.text for item in inbox.close()]


def drop_live_inbox(session: Any, request_id: str | None = None) -> None:
    """What was added goes with the run: the one ``/chat/abort`` actually
    cancelled, or every run, for ``/chat/clear``. An abort that cancelled
    nothing must drop nothing; the run's own seal hands its leftovers back."""
    keys = [request_id] if request_id else list(session.live_inboxes)
    for key in keys:
        entry = session.live_inboxes.pop(key, None)
        if entry is not None:
            entry[0].close()


def single_event_response(event: str) -> StreamingResponse:
    """The answer to a follow-up: one SSE event. The Helm proxy pipes an engine
    body through as ``text/event-stream`` whatever it is, so it stays SSE."""

    async def body() -> Any:
        yield f"event: {event}\ndata: {json.dumps({'event': event})}\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")
