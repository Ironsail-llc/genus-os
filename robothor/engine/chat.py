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
  POST /chat/plan/iterate — Revise pending plan with feedback (keeps same plan_id)
  GET  /chat/plan/status  — Check plan state for a session
  POST /chat/deep/start   — Start deep reasoning (RLM), return SSE stream
  GET  /chat/deep/status  — Check active deep reasoning state
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from robothor.engine.chat_store import (
    clear_plan_state_async,
    clear_session_async,
    load_all_sessions,
    save_exchange_async,
    save_message_async,
    save_plan_state_async,
)
from robothor.engine.models import PLAN_TTL_SECONDS, DeepRunState, PlanState, TriggerType

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
    active_deep: DeepRunState | None = None


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
            # Hydrate pending plan if present and not expired
            plan_data = data.get("plan_state")
            if plan_data and isinstance(plan_data, dict):
                plan = PlanState(
                    plan_id=plan_data.get("plan_id", ""),
                    plan_text=plan_data.get("plan_text", ""),
                    original_message=plan_data.get("original_message", ""),
                    status=plan_data.get("status", "pending"),
                    created_at=plan_data.get("created_at", ""),
                    exploration_run_id=plan_data.get("exploration_run_id", ""),
                    rejection_feedback=plan_data.get("rejection_feedback", ""),
                    revision_count=plan_data.get("revision_count", 0),
                    revision_history=plan_data.get("revision_history", []),
                    execution_run_id=plan_data.get("execution_run_id", ""),
                )
                if plan.status == "pending" and not _plan_is_expired(plan):
                    session.active_plan = plan
                    logger.info("Restored pending plan %s for session %s", plan.plan_id, key)
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

            async def on_tool(event: dict[str, Any]) -> None:
                await queue.put({"event": event["event"], "data": event})

            async def on_status(event: dict[str, Any]) -> None:
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
                on_status=on_status,
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
                        "total_cost_usd": round(run.total_cost_usd, 4),
                        "run_id": run.id,
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

    async def sse_generator() -> AsyncGenerator[str, None]:
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
        "revision_count": plan.revision_count,
        "revision_history": plan.revision_history,
        "execution_run_id": plan.execution_run_id,
        "deep_plan": plan.deep_plan,
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
    deep_plan: bool = body.get("deep_plan", False)

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

            async def on_tool(event: dict[str, Any]) -> None:
                await queue.put({"event": event["event"], "data": event})

            async def on_status(event: dict[str, Any]) -> None:
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
                on_status=on_status,
                model_override=session.model_override,
                conversation_history=list(session.history),
                readonly_mode=True,
                deep_plan=deep_plan,
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
                    deep_plan=deep_plan,
                )
                session.active_plan = plan

                # Persist plan state to DB
                if _config:
                    asyncio.create_task(
                        save_plan_state_async(
                            session_key,
                            _plan_to_dict(plan),
                            tenant_id=_config.tenant_id,
                        )
                    )

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

    async def sse_generator() -> AsyncGenerator[str, None]:
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
        """Execute the approved plan — normal execution or deep reasoning."""
        try:
            if plan.deep_plan:
                # ── Deep plan: route to execute_deep with rich context ──
                # Build context from plan + exploration output (last assistant message)
                exploration_output = ""
                for msg in reversed(session.history):
                    if msg.get("role") == "assistant" and msg.get("content"):
                        exploration_output = msg["content"]
                        break

                context = (
                    f"Original request: {plan.original_message}\n\n"
                    f"Research plan:\n{plan.plan_text}\n\n"
                    f"Exploration output:\n{exploration_output}"
                )

                # Emit deep_start event
                deep_id = str(uuid.uuid4())
                await queue.put(
                    {
                        "event": "deep_start",
                        "data": {"deep_id": deep_id, "query": plan.original_message},
                    }
                )

                async def on_deep_progress(progress: dict[str, Any]) -> None:
                    await queue.put({"event": "deep_progress", "data": progress})

                run = await _runner.execute_deep(
                    query=plan.original_message,
                    on_progress=on_deep_progress,
                    context_override=context,
                )

                # Track execution run ID
                plan.execution_run_id = run.id

                # Merge into history
                session.history.append(
                    {"role": "user", "content": f"[Deep plan executed] {plan.original_message}"}
                )
                if run.output_text:
                    session.history.append({"role": "assistant", "content": run.output_text})
                elif run.error_message:
                    session.history.append(
                        {
                            "role": "assistant",
                            "content": f"[Deep reasoning failed: {run.error_message}]",
                        }
                    )
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

                # Clear plan + persist
                session.active_plan = None
                if _config:
                    asyncio.create_task(
                        clear_plan_state_async(session_key, tenant_id=_config.tenant_id)
                    )

                # Emit deep result + done
                duration_s = (run.duration_ms or 0) / 1000
                cost_usd = run.total_cost_usd or 0.0

                if run.output_text:
                    await queue.put(
                        {
                            "event": "deep_result",
                            "data": {
                                "response": run.output_text,
                                "execution_time_s": round(duration_s, 1),
                                "cost_usd": round(cost_usd, 2),
                            },
                        }
                    )
                    await queue.put(
                        {
                            "event": "done",
                            "data": {
                                "text": run.output_text,
                                "execution_time_s": round(duration_s, 1),
                                "cost_usd": round(cost_usd, 2),
                                "duration_ms": run.duration_ms,
                            },
                        }
                    )
                elif run.error_message:
                    await queue.put({"event": "error", "data": {"error": run.error_message}})
                    await queue.put(
                        {"event": "done", "data": {"text": "", "error": run.error_message}}
                    )
                else:
                    await queue.put(
                        {"event": "done", "data": {"text": "", "duration_ms": run.duration_ms}}
                    )
            else:
                # ── Normal plan execution with full tools ──
                last_sent_len = 0

                async def on_content(cumulative: str) -> None:
                    nonlocal last_sent_len
                    if len(cumulative) > last_sent_len:
                        delta = cumulative[last_sent_len:]
                        last_sent_len = len(cumulative)
                        await queue.put({"event": "delta", "data": {"text": delta}})

                async def on_tool(event: dict[str, Any]) -> None:
                    await queue.put({"event": event["event"], "data": event})

                async def on_status(event: dict[str, Any]) -> None:
                    await queue.put({"event": event["event"], "data": event})

                agent_id = _config.default_chat_agent if _config else "main"
                parts = session_key.split(":")
                if len(parts) >= 2:
                    agent_id = parts[1]

                # CONTEXT RESET — clean execution context, no planning history.
                execution_message = (
                    "Execute the following approved plan. "
                    "Use your tools to carry out each step.\n"
                    "Do NOT re-plan, re-draft, or produce another version. ACT.\n\n"
                    f"Original request: {plan.original_message}\n\n"
                    f"Approved plan:\n{plan.plan_text}"
                )

                run = await _runner.execute(
                    agent_id=agent_id,
                    message=execution_message,
                    trigger_type=TriggerType.WEBCHAT,
                    trigger_detail=f"plan-exec:{session_key}",
                    on_content=on_content,
                    on_tool=on_tool,
                    on_status=on_status,
                    model_override=session.model_override,
                    conversation_history=None,  # CLEAN CONTEXT
                    execution_mode=True,
                )

                # Track execution run ID
                plan.execution_run_id = run.id

                # Merge execution result back into session history for continuity
                session.history.append(
                    {"role": "user", "content": f"[Plan executed] {plan.original_message}"}
                )
                if run.output_text:
                    session.history.append({"role": "assistant", "content": run.output_text})
                elif run.error_message:
                    session.history.append(
                        {"role": "assistant", "content": f"[Execution failed: {run.error_message}]"}
                    )
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

                # Clear plan + persist
                session.active_plan = None
                if _config:
                    asyncio.create_task(
                        clear_plan_state_async(session_key, tenant_id=_config.tenant_id)
                    )

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

    async def sse_generator() -> AsyncGenerator[str, None]:
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

    # Persist cleared state
    if _config:
        asyncio.create_task(clear_plan_state_async(session_key, tenant_id=_config.tenant_id))

    return JSONResponse({"ok": True})


@router.post("/plan/iterate", response_model=None)
async def plan_iterate(request: Request) -> StreamingResponse | JSONResponse:
    """Iterate on a pending plan with feedback — revise without restarting."""
    if _runner is None or _config is None:
        return JSONResponse({"error": "Chat not initialized"}, status_code=503)

    body = await request.json()
    session_key: str = body.get("session_key", "")
    plan_id: str = body.get("plan_id", "")
    feedback: str = body.get("feedback", "")

    if not session_key or not plan_id or not feedback:
        return JSONResponse(
            {"error": "session_key, plan_id, and feedback required"}, status_code=400
        )

    session = _get_session(session_key)

    if not session.active_plan or session.active_plan.plan_id != plan_id:
        return JSONResponse({"error": "No matching pending plan"}, status_code=404)

    if _plan_is_expired(session.active_plan):
        session.active_plan.status = "expired"
        session.active_plan = None
        return JSONResponse({"error": "Plan expired"}, status_code=410)

    plan = session.active_plan

    # Save current plan to revision history
    plan.revision_history.append(
        {
            "plan_text": plan.plan_text,
            "feedback": feedback,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    plan.revision_count += 1

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def run_iteration() -> None:
        """Revise the plan with read-only tools."""
        try:
            last_sent_len = 0

            async def on_content(cumulative: str) -> None:
                nonlocal last_sent_len
                if len(cumulative) > last_sent_len:
                    delta = cumulative[last_sent_len:]
                    last_sent_len = len(cumulative)
                    await queue.put({"event": "delta", "data": {"text": delta}})

            async def on_tool(event: dict[str, Any]) -> None:
                await queue.put({"event": event["event"], "data": event})

            async def on_status(event: dict[str, Any]) -> None:
                await queue.put({"event": event["event"], "data": event})

            agent_id = _config.default_chat_agent if _config else "main"
            parts = session_key.split(":")
            if len(parts) >= 2:
                agent_id = parts[1]

            iteration_message = (
                "[PLAN REVISION]\n"
                "The user reviewed your plan and gave this feedback:\n"
                f'"{feedback}"\n\n'
                f"Current plan:\n{plan.plan_text}\n\n"
                "Revise the plan to address their feedback. "
                "Keep everything they didn't object to.\n"
                'Start with "Changes:" summarizing what you changed.\n'
                "End with [PLAN_READY]."
            )

            run = await _runner.execute(
                agent_id=agent_id,
                message=iteration_message,
                trigger_type=TriggerType.WEBCHAT,
                trigger_detail=f"plan-revise:{session_key}",
                on_content=on_content,
                on_tool=on_tool,
                on_status=on_status,
                model_override=session.model_override,
                conversation_history=list(session.history),
                readonly_mode=True,
            )

            revised_plan_text = _extract_plan_text(run.output_text or "")

            # Update history
            session.history.append({"role": "user", "content": feedback})
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            if len(session.history) > MAX_HISTORY:
                session.history[:] = session.history[-MAX_HISTORY:]

            if revised_plan_text:
                plan.plan_text = revised_plan_text

                # Persist updated plan state
                asyncio.create_task(
                    save_plan_state_async(
                        session_key,
                        _plan_to_dict(plan),
                        tenant_id=_config.tenant_id,
                    )
                )

                await queue.put(
                    {
                        "event": "plan",
                        "data": _plan_to_dict(plan),
                    }
                )

            await queue.put(
                {
                    "event": "done",
                    "data": {
                        "text": run.output_text or "",
                        "model": run.model_used,
                        "input_tokens": run.input_tokens,
                        "output_tokens": run.output_tokens,
                        "duration_ms": run.duration_ms,
                        "plan_id": plan.plan_id,
                        "revision_count": plan.revision_count,
                    },
                }
            )
        except asyncio.CancelledError:
            await queue.put({"event": "done", "data": {"text": "", "aborted": True}})
        except Exception as e:
            logger.error("Plan iteration error: %s", e, exc_info=True)
            await queue.put({"event": "error", "data": {"error": str(e)}})
        finally:
            await queue.put(None)
            session.active_task = None

    task = asyncio.create_task(run_iteration())
    session.active_task = task

    async def sse_generator() -> AsyncGenerator[str, None]:
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


# ─── Deep Mode Endpoints ─────────────────────────────────────────────


def _deep_to_dict(deep: DeepRunState) -> dict[str, Any]:
    """Serialize DeepRunState for JSON responses."""
    return {
        "deep_id": deep.deep_id,
        "query": deep.query,
        "status": deep.status,
        "started_at": deep.started_at,
        "completed_at": deep.completed_at,
        "response": deep.response,
        "execution_time_s": deep.execution_time_s,
        "cost_usd": deep.cost_usd,
        "context_chars": deep.context_chars,
        "trajectory_file": deep.trajectory_file,
        "error": deep.error,
    }


@router.post("/deep/start", response_model=None)
async def deep_start(request: Request) -> StreamingResponse | JSONResponse:
    """Start deep reasoning: call RLM directly, return SSE stream with progress."""
    if _runner is None or _config is None:
        return JSONResponse({"error": "Chat not initialized"}, status_code=503)

    body = await request.json()
    session_key: str = body.get("session_key", "")
    query: str = (body.get("query", "") or body.get("message", "")).strip()

    if not session_key or not query:
        return JSONResponse({"error": "session_key and query required"}, status_code=400)

    session = _get_session(session_key)

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def run_deep() -> None:
        """Execute RLM in background, push progress events to queue."""
        deep_id = str(uuid.uuid4())
        deep = DeepRunState(
            deep_id=deep_id,
            query=query,
            status="running",
            started_at=datetime.now(UTC).isoformat(),
        )
        session.active_deep = deep

        try:
            # Acknowledge start
            await queue.put({"event": "deep_start", "data": {"deep_id": deep_id, "query": query}})

            async def on_progress(progress: dict[str, Any]) -> None:
                await queue.put({"event": "deep_progress", "data": progress})

            run = await _runner.execute_deep(
                query=query,
                on_progress=on_progress,
                conversation_history=list(session.history),
            )

            deep.completed_at = datetime.now(UTC).isoformat()

            if run.error_message:
                deep.status = "failed"
                deep.error = run.error_message
                await queue.put({"event": "error", "data": {"error": run.error_message}})
            else:
                deep.status = "completed"
                deep.response = run.output_text or ""
                deep.execution_time_s = (run.duration_ms or 0) / 1000
                deep.cost_usd = run.total_cost_usd

                await queue.put(
                    {
                        "event": "deep_result",
                        "data": {
                            "response": deep.response,
                            "execution_time_s": deep.execution_time_s,
                            "cost_usd": round(deep.cost_usd, 4),
                            "context_chars": deep.context_chars,
                            "trajectory_file": deep.trajectory_file,
                        },
                    }
                )

            # Record in session history for continuity
            session.history.append({"role": "user", "content": f"/deep {query}"})
            if deep.response:
                session.history.append({"role": "assistant", "content": deep.response})
            elif deep.error:
                session.history.append(
                    {"role": "assistant", "content": f"[Deep reasoning failed: {deep.error}]"}
                )
            if len(session.history) > MAX_HISTORY:
                session.history[:] = session.history[-MAX_HISTORY:]

            # Persist to DB
            if run.output_text and _config:
                asyncio.create_task(
                    save_exchange_async(
                        session_key,
                        f"/deep {query}",
                        run.output_text,
                        channel="webchat",
                        model_override=session.model_override,
                        tenant_id=_config.tenant_id,
                    )
                )

            # Signal completion
            await queue.put(
                {
                    "event": "done",
                    "data": {
                        "text": run.output_text or "",
                        "execution_time_s": deep.execution_time_s,
                        "cost_usd": round(deep.cost_usd, 4),
                        "run_id": run.id,
                        "total_cost_usd": round(run.total_cost_usd, 4),
                    },
                }
            )
        except asyncio.CancelledError:
            deep.status = "failed"
            deep.error = "Cancelled"
            await queue.put({"event": "done", "data": {"text": "", "aborted": True}})
        except Exception as e:
            logger.error("Deep reasoning error: %s", e, exc_info=True)
            deep.status = "failed"
            deep.error = str(e)
            await queue.put({"event": "error", "data": {"error": str(e)}})
        finally:
            await queue.put(None)
            session.active_task = None
            session.active_deep = None

    task = asyncio.create_task(run_deep())
    session.active_task = task

    async def sse_generator() -> AsyncGenerator[str, None]:
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


@router.get("/deep/status")
async def deep_status(session_key: str = "") -> JSONResponse:
    """Check deep reasoning state for a session."""
    if not session_key:
        return JSONResponse({"error": "session_key required"}, status_code=400)

    session = _get_session(session_key)

    if session.active_deep:
        return JSONResponse(
            {
                "active": True,
                "deep": _deep_to_dict(session.active_deep),
            }
        )
    return JSONResponse({"active": False, "deep": None})
