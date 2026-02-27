"""
HTTP chat endpoints — SSE-streaming webchat for the Helm.

In-memory session store with conversation history.
Mirrors the Telegram bot's pattern: one active response per session,
conversation history trimmed to MAX_HISTORY entries.

Endpoints:
  POST /chat/send    — Accept message, return SSE stream (delta/done/error)
  GET  /chat/history — Return session conversation history
  POST /chat/inject  — Add system message to session
  POST /chat/abort   — Cancel running response
  POST /chat/clear   — Reset session history
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from robothor.engine.models import TriggerType

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat")

MAX_HISTORY = 40  # 20 turns (user + assistant)

# Module-level references injected by init_chat()
_runner: AgentRunner | None = None
_config: EngineConfig | None = None


@dataclass
class ChatSession:
    """Per-session chat state."""

    history: list[dict[str, Any]] = field(default_factory=list)
    active_task: asyncio.Task[Any] | None = None
    model_override: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# In-memory session store
_sessions: dict[str, ChatSession] = {}


def _get_session(session_key: str) -> ChatSession:
    if session_key not in _sessions:
        _sessions[session_key] = ChatSession()
    return _sessions[session_key]


def init_chat(runner: AgentRunner, config: EngineConfig) -> None:
    """Initialize module with shared runner and config. Called once from daemon."""
    global _runner, _config
    _runner = runner
    _config = config
    logger.info("Chat endpoints initialized")


@router.post("/send")
async def chat_send(request: Request) -> StreamingResponse:
    """Accept a message and return an SSE stream of deltas."""
    if _runner is None or _config is None:
        return JSONResponse({"error": "Chat not initialized"}, status_code=503)

    body = await request.json()
    session_key: str = body.get("session_key", "")
    message: str = body.get("message", "")

    if not session_key or not message:
        return JSONResponse(
            {"error": "session_key and message required"}, status_code=400
        )

    session = _get_session(session_key)

    # Non-blocking busy check
    if session.lock.locked():
        return JSONResponse(
            {"error": "Session is busy processing another request"}, status_code=409
        )

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def run_agent() -> None:
        """Execute agent in background, push events to queue."""
        async with session.lock:
            try:
                last_sent_len = 0

                async def on_content(cumulative: str) -> None:
                    nonlocal last_sent_len
                    if len(cumulative) > last_sent_len:
                        delta = cumulative[last_sent_len:]
                        last_sent_len = len(cumulative)
                        await queue.put({"event": "delta", "data": {"text": delta}})

                # Determine agent ID from session key or default to "main"
                agent_id = "main"
                parts = session_key.split(":")
                if len(parts) >= 2:
                    agent_id = parts[1]

                run = await _runner.execute(
                    agent_id=agent_id,
                    message=message,
                    trigger_type=TriggerType.WEBCHAT,
                    trigger_detail=f"webchat:{session_key}",
                    on_content=on_content,
                    model_override=session.model_override,
                    conversation_history=list(session.history),
                )

                # Append to session history
                session.history.append({"role": "user", "content": message})
                if run.output_text:
                    session.history.append(
                        {"role": "assistant", "content": run.output_text}
                    )

                # Trim history
                if len(session.history) > MAX_HISTORY:
                    session.history = session.history[-MAX_HISTORY:]

                # Signal completion
                await queue.put(
                    {
                        "event": "done",
                        "data": {"text": run.output_text or ""},
                    }
                )
            except asyncio.CancelledError:
                await queue.put(
                    {"event": "done", "data": {"text": "", "aborted": True}}
                )
            except Exception as e:
                logger.error("Chat agent error: %s", e, exc_info=True)
                await queue.put({"event": "error", "data": {"error": str(e)}})
            finally:
                await queue.put(None)  # Sentinel
                session.active_task = None

    # Start agent as background task
    task = asyncio.create_task(run_agent())
    session.active_task = task

    async def sse_generator():
        """Yield SSE events from the queue."""
        import json

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                event = item["event"]
                data = json.dumps(item["data"])
                yield f"event: {event}\ndata: {data}\n\n"
        except asyncio.CancelledError:
            # Client disconnected
            task.cancel()
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/history")
async def chat_history(session_key: str = "", limit: int = 50) -> JSONResponse:
    """Return conversation history for a session."""
    if not session_key:
        return JSONResponse({"error": "session_key required"}, status_code=400)

    session = _get_session(session_key)
    messages = session.history[-limit:] if limit > 0 else session.history

    return JSONResponse(
        {"sessionKey": session_key, "messages": messages}
    )


@router.post("/inject")
async def chat_inject(request: Request) -> JSONResponse:
    """Add a system message to the session history."""
    body = await request.json()
    session_key: str = body.get("session_key", "")
    message: str = body.get("message", "")
    label: str = body.get("label", "")

    if not session_key or not message:
        return JSONResponse(
            {"error": "session_key and message required"}, status_code=400
        )

    session = _get_session(session_key)
    session.history.append({"role": "system", "content": message})

    logger.debug("Injected system message into %s (label=%s)", session_key, label)
    return JSONResponse({"ok": True})


@router.post("/abort")
async def chat_abort(request: Request) -> JSONResponse:
    """Cancel the running response for a session."""
    body = await request.json()
    session_key: str = body.get("session_key", "")

    if not session_key:
        return JSONResponse({"error": "session_key required"}, status_code=400)

    session = _get_session(session_key)
    aborted = False

    if session.active_task and not session.active_task.done():
        session.active_task.cancel()
        aborted = True

    return JSONResponse({"ok": True, "aborted": aborted})


@router.post("/clear")
async def chat_clear(request: Request) -> JSONResponse:
    """Reset session history."""
    body = await request.json()
    session_key: str = body.get("session_key", "")

    if not session_key:
        return JSONResponse({"error": "session_key required"}, status_code=400)

    session = _get_session(session_key)

    # Cancel any active task first
    if session.active_task and not session.active_task.done():
        session.active_task.cancel()

    session.history.clear()
    session.model_override = None

    return JSONResponse({"ok": True})
