"""
HTTP chat endpoints — SSE-streaming webchat for the Helm.

In-memory session store with conversation history.
Mirrors the Telegram bot's pattern: one active response per session,
conversation history trimmed to MAX_HISTORY entries.

Endpoints:
  POST /chat/send       — Accept message, return SSE stream (delta/done/error)
  GET  /chat/history    — Return session conversation history
  POST /chat/inject     — Add system message to session
  POST /chat/abort      — Cancel running response
  POST /chat/clear      — Reset session history
  POST /chat/plan/start   — Start plan mode: explore with read-only tools
  POST /chat/plan/approve — Approve pending plan: execute with full tools
  POST /chat/plan/reject  — Reject pending plan (optional feedback)
  GET  /chat/plan/status  — Check plan state for a session
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from robothor.engine.chat_store import (
    clear_session_async,
    load_all_sessions,
    save_exchange_async,
    save_message_async,
)
from robothor.engine.models import PLAN_TTL_SECONDS, PlanState, TriggerType

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat")

MAX_HISTORY = 40  # 20 turns (user + assistant)
SSE_KEEPALIVE_INTERVAL = 15.0  # seconds between keepalive comments

# Module-level references injected by init_chat()
_runner: AgentRunner | None = None
_config: EngineConfig | None = None


@dataclass
class ChatSession:
    """Per-session chat state."""

    history: list[dict[str, Any]] = field(default_factory=list)
    active_task: asyncio.Task[Any] | None = None
    model_override: str | None = None
    plan_mode: bool = False
    active_plan: PlanState | None = None


# In-memory session store
_sessions: dict[str, ChatSession] = {}


def _get_session(session_key: str) -> ChatSession:
    if session_key not in _sessions:
        _sessions[session_key] = ChatSession()
    return _sessions[session_key]


def get_shared_session(session_key: str) -> ChatSession:
    """Public accessor — returns (or creates) the ChatSession for *session_key*.

    Used by telegram.py so both channels share one in-memory session.
    """
    return _get_session(session_key)


def get_main_session_key() -> str:
    """Return the canonical session key configured in EngineConfig."""
    if _config is not None:
        return _config.main_session_key
    return "agent:main:primary"


def _restore_sessions(config: EngineConfig) -> None:
    """Restore webchat sessions from PostgreSQL at startup."""
    try:
        sessions = load_all_sessions(
            limit_per_session=MAX_HISTORY,
            tenant_id=config.tenant_id,
        )
        restored = 0
        for key, data in sessions.items():
            session = _get_session(key)
            history = data.get("history", [])
            if history:
                session.history = history
            model = data.get("model_override")
            if model:
                session.model_override = model
            restored += 1
        if restored:
            logger.info("Restored %d chat sessions from DB", restored)
    except Exception as e:
        logger.warning("Failed to load persisted webchat sessions: %s", e)


def init_chat(runner: AgentRunner, config: EngineConfig) -> None:
    """Initialize module with shared runner and config. Called once from daemon."""
    global _runner, _config
    _runner = runner
    _config = config
    _restore_sessions(config)
    logger.info("Chat endpoints initialized")


@router.post("/send", response_model=None)
async def chat_send(request: Request) -> StreamingResponse | JSONResponse:
    """Accept a message and return an SSE stream of deltas."""
    if _runner is None or _config is None:
        return JSONResponse({"error": "Chat not initialized"}, status_code=503)

    body = await request.json()
    session_key: str = body.get("session_key", "")
    message: str = body.get("message", "")

    if not session_key or not message:
        return JSONResponse({"error": "session_key and message required"}, status_code=400)

    session = _get_session(session_key)

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def run_agent() -> None:
        """Execute agent in background, push events to queue."""
        try:
            last_sent_len = 0

            async def on_content(cumulative: str) -> None:
                nonlocal last_sent_len
                if len(cumulative) > last_sent_len:
                    delta = cumulative[last_sent_len:]
                    last_sent_len = len(cumulative)
                    await queue.put({"event": "delta", "data": {"text": delta}})

            async def on_tool(event: dict) -> None:
                await queue.put({"event": event["event"], "data": event})

            # Determine agent ID from session key or default
            agent_id = _config.default_chat_agent if _config else "main"
            parts = session_key.split(":")
            if len(parts) >= 2:
                agent_id = parts[1]

            run = await _runner.execute(
                agent_id=agent_id,
                message=message,
                trigger_type=TriggerType.WEBCHAT,
                trigger_detail=f"webchat:{session_key}",
                on_content=on_content,
                on_tool=on_tool,
                model_override=session.model_override,
                conversation_history=list(session.history),
            )

            # Always record user message in session history
            session.history.append({"role": "user", "content": message})
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            elif run.error_message:
                # Record error so the next run knows what failed
                session.history.append(
                    {
                        "role": "assistant",
                        "content": f"[Run failed: {run.error_message}]",
                    }
                )

            # Trim history (in-place slice for safety under concurrency)
            if len(session.history) > MAX_HISTORY:
                session.history[:] = session.history[-MAX_HISTORY:]

            # Persist to DB (fire-and-forget)
            if run.output_text and _config:
                asyncio.create_task(
                    save_exchange_async(
                        session_key,
                        message,
                        run.output_text,
                        channel="webchat",
                        model_override=session.model_override,
                        tenant_id=_config.tenant_id,
                    )
                )

            # Signal completion with metadata
            await queue.put(
                {
                    "event": "done",
                    "data": {
                        "text": run.output_text or "",
                        "model": run.model_used,
                        "input_tokens": run.input_tokens,
                        "output_tokens": run.output_tokens,
                        "duration_ms": run.duration_ms,
                    },
                }
            )
        except asyncio.CancelledError:
            await queue.put({"event": "done", "data": {"text": "", "aborted": True}})
        except Exception as e:
            logger.error("Chat agent error: %s", e, exc_info=True)
            # Record the failed attempt so next run has context
            session.history.append({"role": "user", "content": message})
            session.history.append(
                {
                    "role": "assistant",
                    "content": f"[Internal error — run failed: {e}]",
                }
            )
            if len(session.history) > MAX_HISTORY:
                session.history[:] = session.history[-MAX_HISTORY:]
            await queue.put({"event": "error", "data": {"error": str(e)}})
        finally:
            await queue.put(None)  # Sentinel
            session.active_task = None

    # Start agent as background task
    task = asyncio.create_task(run_agent())
    session.active_task = task

    async def sse_generator():
        """Yield SSE events from the queue, with keepalive comments."""
        import json

        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_INTERVAL)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
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

    return JSONResponse({"sessionKey": session_key, "messages": messages})


@router.post("/inject")
async def chat_inject(request: Request) -> JSONResponse:
    """Add a system message to the session history."""
    body = await request.json()
    session_key: str = body.get("session_key", "")
    message: str = body.get("message", "")
    label: str = body.get("label", "")

    if not session_key or not message:
        return JSONResponse({"error": "session_key and message required"}, status_code=400)

    session = _get_session(session_key)
    session.history.append({"role": "system", "content": message})

    # Persist to DB (fire-and-forget)
    if _config:
        asyncio.create_task(
            save_message_async(
                session_key,
                "system",
                message,
                channel="webchat",
                tenant_id=_config.tenant_id,
            )
        )

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

    # Also clear any pending plan
    session.active_plan = None
    session.plan_mode = False

    # Persist to DB (fire-and-forget)
    if _config:
        asyncio.create_task(
            clear_session_async(
                session_key,
                tenant_id=_config.tenant_id,
            )
        )

    return JSONResponse({"ok": True})


# ─── Plan Mode Helpers ────────────────────────────────────────────────


def _plan_is_expired(plan: PlanState) -> bool:
    """Check if a pending plan has exceeded its TTL."""
    if not plan.created_at:
        return True
    try:
        created = datetime.fromisoformat(plan.created_at)
        elapsed = (datetime.now(UTC) - created).total_seconds()
        return elapsed > PLAN_TTL_SECONDS
    except (ValueError, TypeError):
        return True


def _extract_plan_text(output: str) -> str:
    """Extract plan text from agent output, stripping the [PLAN_READY] marker."""
    if not output:
        return ""
    marker = "[PLAN_READY]"
    idx = output.find(marker)
    if idx != -1:
        return output[:idx].strip()
    return output.strip()


def _plan_to_dict(plan: PlanState) -> dict[str, Any]:
    """Serialize PlanState for JSON responses."""
    return {
        "plan_id": plan.plan_id,
        "plan_text": plan.plan_text,
        "original_message": plan.original_message,
        "status": plan.status,
        "created_at": plan.created_at,
        "exploration_run_id": plan.exploration_run_id,
        "rejection_feedback": plan.rejection_feedback,
    }


# ─── Plan Mode Endpoints ─────────────────────────────────────────────


@router.post("/plan/start", response_model=None)
async def plan_start(request: Request) -> StreamingResponse | JSONResponse:
    """Start plan mode: run agent with read-only tools, return plan via SSE."""
    if _runner is None or _config is None:
        return JSONResponse({"error": "Chat not initialized"}, status_code=503)

    body = await request.json()
    session_key: str = body.get("session_key", "")
    message: str = body.get("message", "")

    if not session_key or not message:
        return JSONResponse({"error": "session_key and message required"}, status_code=400)

    session = _get_session(session_key)

    # Expire stale plan if any
    if session.active_plan and _plan_is_expired(session.active_plan):
        session.active_plan.status = "expired"
        session.active_plan = None

    # Supersede any pending plan (revision flow — new plan replaces old)
    if session.active_plan and session.active_plan.status == "pending":
        session.active_plan.status = "superseded"
        session.active_plan = None

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def run_plan_agent() -> None:
        """Execute agent in plan mode (readonly), push events to queue."""
        try:
            last_sent_len = 0

            async def on_content(cumulative: str) -> None:
                nonlocal last_sent_len
                if len(cumulative) > last_sent_len:
                    delta = cumulative[last_sent_len:]
                    last_sent_len = len(cumulative)
                    await queue.put({"event": "delta", "data": {"text": delta}})

            async def on_tool(event: dict) -> None:
                await queue.put({"event": event["event"], "data": event})

            agent_id = _config.default_chat_agent if _config else "main"
            parts = session_key.split(":")
            if len(parts) >= 2:
                agent_id = parts[1]

            run = await _runner.execute(
                agent_id=agent_id,
                message=message,
                trigger_type=TriggerType.WEBCHAT,
                trigger_detail=f"plan:{session_key}",
                on_content=on_content,
                on_tool=on_tool,
                model_override=session.model_override,
                conversation_history=list(session.history),
                readonly_mode=True,
            )

            # Extract plan from output
            plan_text = _extract_plan_text(run.output_text or "")

            # Accumulate history so revisions have full context
            session.history.append({"role": "user", "content": message})
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            if len(session.history) > MAX_HISTORY:
                session.history[:] = session.history[-MAX_HISTORY:]
            if run.output_text and _config:
                asyncio.create_task(
                    save_exchange_async(
                        session_key,
                        message,
                        run.output_text,
                        channel="webchat",
                        model_override=session.model_override,
                        tenant_id=_config.tenant_id,
                    )
                )

            if plan_text:
                plan = PlanState(
                    plan_id=str(uuid.uuid4()),
                    plan_text=plan_text,
                    original_message=message,
                    status="pending",
                    created_at=datetime.now(UTC).isoformat(),
                    exploration_run_id=run.id,
                )
                session.active_plan = plan

                # Send plan event
                await queue.put(
                    {
                        "event": "plan",
                        "data": _plan_to_dict(plan),
                    }
                )

            # Signal completion
            await queue.put(
                {
                    "event": "done",
                    "data": {
                        "text": run.output_text or "",
                        "model": run.model_used,
                        "input_tokens": run.input_tokens,
                        "output_tokens": run.output_tokens,
                        "duration_ms": run.duration_ms,
                        "plan_id": session.active_plan.plan_id if session.active_plan else None,
                    },
                }
            )
        except asyncio.CancelledError:
            await queue.put({"event": "done", "data": {"text": "", "aborted": True}})
        except Exception as e:
            logger.error("Plan agent error: %s", e, exc_info=True)
            await queue.put({"event": "error", "data": {"error": str(e)}})
        finally:
            await queue.put(None)
            session.active_task = None

    task = asyncio.create_task(run_plan_agent())
    session.active_task = task

    async def sse_generator():
        import json as _json

        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_INTERVAL)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    break
                event = item["event"]
                data = _json.dumps(item["data"])
                yield f"event: {event}\ndata: {data}\n\n"
        except asyncio.CancelledError:
            task.cancel()
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.post("/plan/approve", response_model=None)
async def plan_approve(request: Request) -> StreamingResponse | JSONResponse:
    """Approve a pending plan: execute original message with full tools."""
    if _runner is None or _config is None:
        return JSONResponse({"error": "Chat not initialized"}, status_code=503)

    body = await request.json()
    session_key: str = body.get("session_key", "")
    plan_id: str = body.get("plan_id", "")

    if not session_key or not plan_id:
        return JSONResponse({"error": "session_key and plan_id required"}, status_code=400)

    session = _get_session(session_key)

    if not session.active_plan or session.active_plan.plan_id != plan_id:
        return JSONResponse({"error": "No matching pending plan"}, status_code=404)

    if _plan_is_expired(session.active_plan):
        session.active_plan.status = "expired"
        session.active_plan = None
        return JSONResponse({"error": "Plan expired"}, status_code=410)

    plan = session.active_plan
    plan.status = "approved"

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def run_approved() -> None:
        """Execute the approved plan with full tools."""
        try:
            last_sent_len = 0

            async def on_content(cumulative: str) -> None:
                nonlocal last_sent_len
                if len(cumulative) > last_sent_len:
                    delta = cumulative[last_sent_len:]
                    last_sent_len = len(cumulative)
                    await queue.put({"event": "delta", "data": {"text": delta}})

            async def on_tool(event: dict) -> None:
                await queue.put({"event": event["event"], "data": event})

            agent_id = _config.default_chat_agent if _config else "main"
            parts = session_key.split(":")
            if len(parts) >= 2:
                agent_id = parts[1]

            # Inject plan as context so the agent knows what was approved
            plan_context = (
                f"[APPROVED PLAN] The following plan was approved. Execute it now.\n\n"
                f"{plan.plan_text}"
            )
            history = list(session.history)
            history.append({"role": "system", "content": plan_context})

            run = await _runner.execute(
                agent_id=agent_id,
                message=plan.original_message,
                trigger_type=TriggerType.WEBCHAT,
                trigger_detail=f"plan-exec:{session_key}",
                on_content=on_content,
                on_tool=on_tool,
                model_override=session.model_override,
                conversation_history=history,
            )

            # Update session history
            session.history.append({"role": "user", "content": plan.original_message})
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            if len(session.history) > MAX_HISTORY:
                session.history[:] = session.history[-MAX_HISTORY:]

            # Persist to DB
            if run.output_text and _config:
                asyncio.create_task(
                    save_exchange_async(
                        session_key,
                        plan.original_message,
                        run.output_text,
                        channel="webchat",
                        model_override=session.model_override,
                        tenant_id=_config.tenant_id,
                    )
                )

            # Clear plan
            session.active_plan = None

            await queue.put(
                {
                    "event": "done",
                    "data": {
                        "text": run.output_text or "",
                        "model": run.model_used,
                        "input_tokens": run.input_tokens,
                        "output_tokens": run.output_tokens,
                        "duration_ms": run.duration_ms,
                    },
                }
            )
        except asyncio.CancelledError:
            await queue.put({"event": "done", "data": {"text": "", "aborted": True}})
        except Exception as e:
            logger.error("Plan execution error: %s", e, exc_info=True)
            await queue.put({"event": "error", "data": {"error": str(e)}})
        finally:
            await queue.put(None)
            session.active_task = None

    task = asyncio.create_task(run_approved())
    session.active_task = task

    async def sse_generator():
        import json as _json

        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_INTERVAL)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    break
                event = item["event"]
                data = _json.dumps(item["data"])
                yield f"event: {event}\ndata: {data}\n\n"
        except asyncio.CancelledError:
            task.cancel()
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.post("/plan/reject")
async def plan_reject(request: Request) -> JSONResponse:
    """Reject a pending plan, optionally with feedback."""
    body = await request.json()
    session_key: str = body.get("session_key", "")
    plan_id: str = body.get("plan_id", "")
    feedback: str = body.get("feedback", "")

    if not session_key or not plan_id:
        return JSONResponse({"error": "session_key and plan_id required"}, status_code=400)

    session = _get_session(session_key)

    if not session.active_plan or session.active_plan.plan_id != plan_id:
        return JSONResponse({"error": "No matching pending plan"}, status_code=404)

    session.active_plan.status = "rejected"
    session.active_plan.rejection_feedback = feedback

    # Inject rejection feedback into session so agent can learn
    if feedback:
        session.history.append(
            {
                "role": "system",
                "content": f"[PLAN REJECTED] The previous plan was rejected. Feedback: {feedback}",
            }
        )

    session.active_plan = None
    return JSONResponse({"ok": True})


@router.get("/plan/status")
async def plan_status(session_key: str = "") -> JSONResponse:
    """Check plan state for a session."""
    if not session_key:
        return JSONResponse({"error": "session_key required"}, status_code=400)

    session = _get_session(session_key)

    # Auto-expire stale plans
    if session.active_plan and _plan_is_expired(session.active_plan):
        session.active_plan.status = "expired"
        session.active_plan = None

    if session.active_plan:
        return JSONResponse(
            {
                "active": True,
                "plan": _plan_to_dict(session.active_plan),
            }
        )
    return JSONResponse({"active": False, "plan": None})
