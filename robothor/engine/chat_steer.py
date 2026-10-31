"""Steer and stop one webchat turn's live run (``ROBOTHOR_STEER_EVERYWHERE``).

``POST /chat/steer`` and ``POST /chat/interrupt`` reach the run of the turn the
body names by ``request_id``; ``GET /chat/steer`` says whether the switch is on.
Included into the chat router (``chat.py``), so they share its ``/chat`` prefix
and its authentication dependency. See ``live_control.py`` for how a turn's run
is found, and ``docs/channels/webchat.md`` for the behaviour.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from robothor.engine import live_control

logger = logging.getLogger(__name__)

router = APIRouter()


def turn_key(auth: Any, session_key: str, body: dict[str, Any]) -> str:
    """The key ``/chat/send`` tags its run with; ``""`` (untagged) without a request id.

    Only a turn that names its request id can be steered or stopped.
    """
    request_id = body.get("request_id")
    return webchat_run_key(auth, session_key, request_id) if request_id else ""


def webchat_run_key(auth: Any, session_key: str, request_id: Any) -> str:
    """The live-run key of ONE webchat turn: tenant, user, session and request id.

    A session key alone is not enough. Webchat runs several turns on one key at
    once (two tabs, two turns), and the owner's and service callers' keys are
    shared, so steer and interrupt name the turn the way ``/chat/abort`` does
    (``chat_control.request_key``), and only the user who sent it can reach it.
    """
    from robothor.engine.runtime.chat_control import request_key

    return f"webchat:{request_key(auth, session_key, request_id)}"


@router.get("/steer")
async def chat_steer_mode() -> JSONResponse:
    """Whether steer and interrupt are on (``ROBOTHOR_STEER_EVERYWHERE``)."""
    return JSONResponse({"enabled": live_control.steer_everywhere_enabled()})


@router.post("/steer")
async def chat_steer(request: Request) -> JSONResponse:
    """Steer one turn's live run: the text joins it at the next iteration boundary.

    The body names the turn by the ``request_id`` it was sent with, and the run
    is found by :func:`webchat_run_key` (tenant, user, session, request), so a
    caller can steer only a turn they sent. ``found`` is false when that turn
    has no live run.
    """
    if not live_control.steer_everywhere_enabled():
        return JSONResponse({"error": "not found"}, status_code=404)
    from robothor.engine import chat

    auth = chat._auth_context(request)
    body = await request.json()
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    if not body.get("request_id"):
        return JSONResponse({"error": "request_id required"}, status_code=400)
    session_key = chat._effective_session_key(auth, body.get("session_key", ""))
    key = webchat_run_key(auth, session_key, body["request_id"])
    # Off the loop: the steer is recorded as an operator signal in the database.
    found = await asyncio.to_thread(
        live_control.steer_conversation, key, text, tenant_id=auth.tenant_id
    )
    return JSONResponse({"ok": found, "found": found})


@router.post("/interrupt")
async def chat_interrupt(request: Request) -> JSONResponse:
    """Stop one turn's live run the way the runs API and Telegram's Interrupt do.

    It goes through ``interrupt_api.interrupt_session``: the run's interrupt
    flag (honoured at the next iteration boundary, where the run ends as
    CANCELLED) and the durable runtime stop, which may end the run's task
    sooner. The turn is named by ``request_id``, as for :func:`chat_steer`.
    """
    if not live_control.steer_everywhere_enabled():
        return JSONResponse({"error": "not found"}, status_code=404)
    from robothor.engine import chat

    auth = chat._auth_context(request)
    body = await request.json()
    if not body.get("request_id"):
        return JSONResponse({"error": "request_id required"}, status_code=400)
    session_key = chat._effective_session_key(auth, body.get("session_key", ""))
    key = webchat_run_key(auth, session_key, body["request_id"])
    message = body.get("message")
    try:
        # Off the loop: the durable stop is written to the database.
        found = await asyncio.to_thread(
            live_control.interrupt_conversation,
            key,
            str(message) if message else None,
            tenant_id=auth.tenant_id,
        )
    except Exception:
        logger.exception("Chat interrupt could not be recorded")
        return JSONResponse({"error": "interrupt could not be recorded"}, status_code=502)
    return JSONResponse({"ok": found, "found": found})
