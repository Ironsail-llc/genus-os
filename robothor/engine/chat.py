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

The in-memory ``_sessions`` dict is a bounded cache over the PostgreSQL store,
not the truth. Each session's *history* is capped at MAX_HISTORY, and the
number of sessions is capped at MAX_SESSIONS: creating a key past the cap
evicts the least-recently-used session that has no running task, no pending
plan and no deep run (the main session is never evicted). An evicted key is
rehydrated from the store on its next message, so nothing a member said is
lost — only the memory it occupied while idle. ``evict_idle_sessions`` is the
TTL sweep for callers that want one.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from robothor.constants import DEFAULT_TENANT
from robothor.engine.chat_history import MAX_HISTORY as _MAX_HISTORY
from robothor.engine.chat_history import ChatHistory, append_turn, as_history
from robothor.engine.chat_plan_claim import (
    admit_plan,
    approval_refusal,
    approval_retry,
    finish_plan,
)
from robothor.engine.chat_result import result_text
from robothor.engine.chat_session_cache import SessionCache
from robothor.engine.chat_store import (
    clear_plan_state_async,
    clear_session_async,
    load_all_sessions,
    load_session,
    save_exchange_async,
    save_message_async,
    save_plan_state_async,
)
from robothor.engine.feature_flags import per_user_sessions_mode
from robothor.engine.models import PLAN_TTL_SECONDS, DeepRunState, PlanState, RunStatus, TriggerType
from robothor.engine.runtime.chat_control import start, stop
from robothor.engine.sanitize import sanitize_log

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner
    from robothor.identity import IdentityContext

logger = logging.getLogger(__name__)

#: Re-exported from ``chat_history``, which owns it now: the cap and the
#: container it bounds are one concern, and several modules import it from here.
MAX_HISTORY = _MAX_HISTORY
SSE_KEEPALIVE_INTERVAL = 15.0  # seconds between keepalive comments

# Module-level references injected by init_chat()
_runner: AgentRunner | None = None
_config: EngineConfig | None = None


def _require_chat_auth(request: Request) -> None:
    """Route-level auth so the router is safe even when mounted standalone."""

    from robothor.auth.deps import AuthContext
    from robothor.engine.auth import authenticate_http_request

    if isinstance(getattr(request.state, "auth", None), AuthContext):
        return
    if _config is None:
        raise HTTPException(status_code=503, detail="Chat not initialized")
    context = authenticate_http_request(request, tenant_id=_config.tenant_id)
    if context is None:
        raise HTTPException(status_code=401, detail="authentication required")
    request.state.auth = context


def _auth_context(request: Request) -> Any:
    from robothor.engine.auth import request_context

    return request_context(request)


def _resolve_webchat_identity(auth: Any) -> IdentityContext | None:
    """Resolve the CURRENT USER identity for a webchat request's auth context.

    Service-typ tokens (engine/agent → bridge calls) have no human on the
    other end, so they get identity=None rather than a resolution attempt.
    """
    if getattr(auth, "is_service", False):
        return None

    from robothor.identity import resolve_identity

    return resolve_identity("webchat", auth.user_id, auth.tenant_id)


def derive_user_session_key(agent_id: str, user_id: str) -> str:
    """The session key one member's conversation with ``agent_id`` lives under.

    The ONE place this shape is written. Two callers need it — the request path
    below and :class:`~robothor.engine.channels.webchat.WebchatChannel`, which
    has to write a delivery into the same session the member's own requests land
    in. Derived twice, they would drift the first time either changed and a
    briefing would land in a session nobody reads.
    """
    return f"agent:{agent_id or 'main'}:user:{user_id}"


def _effective_session_key(auth: Any, requested_key: str) -> str:
    """Resolve the session key a chat request should actually operate on.

    Every dashboard user has historically shared ONE engine session, and
    every endpoint below accepted any session_key from any authenticated
    same-tenant caller with no ownership check. This is the fix, gated by
    ``ROBOTHOR_PER_USER_SESSIONS`` (see ``feature_flags.per_user_sessions_mode``):

    - Service-typ tokens (ops tooling, the Telegram bridge writing into
      main's session) always keep ``requested_key`` verbatim, in every flag
      mode — there is no human on the other end to isolate.
    - The tenant owner always keeps ``requested_key`` verbatim, in every
      flag mode — the operator's webchat<->Telegram shared-session
      continuity (``telegram.py``'s ``main_session_key``) and existing
      history must survive this rollout untouched.
    - Everyone else ("member" and other non-owner human roles) is isolated
      onto ``agent:{agent_id}:user:{auth.user_id}`` — but only when the mode
      is ``enforce``. ``agent_id`` is parsed the same way the rest of this
      module derives it from a session key: the ``parts[1]`` segment of
      ``agent:main:primary``, falling back to the configured default agent
      when the requested key doesn't have that shape.
    - ``observe`` mode returns ``requested_key`` unchanged (no behavior
      change) but logs the derivation that enforce mode WOULD have made, so
      the rollout can be evaluated before it changes anyone's session.
    - ``off`` always returns ``requested_key`` unchanged. It is the escape
      hatch, not the default: ``enforce`` is what a fresh instance runs.

    An EMPTY ``requested_key`` is the main session key. The Helm stopped
    sending one at all (there is nothing a browser could say here that the
    server does not already know better), and every rule below then applies to
    that key exactly as it would to one that arrived over the wire — so
    omitting the field is no more a way to choose a session than forging it is.
    """
    requested_key = requested_key or get_main_session_key()
    if getattr(auth, "is_service", False):
        return requested_key

    mode = per_user_sessions_mode()
    if mode == "off":
        return requested_key

    role = getattr(auth, "role", "")
    if role == "owner":
        return requested_key

    parts = requested_key.split(":")
    agent_id = parts[1] if len(parts) >= 2 else (_config.default_chat_agent if _config else "main")
    derived = derive_user_session_key(agent_id, auth.user_id)

    if mode == "observe":
        if derived != requested_key:
            logger.info(
                "per_user_sessions: would derive %s from %s for user=%s role=%s",
                derived,
                requested_key,
                auth.user_id,
                role,
            )
        return requested_key

    return derived


router = APIRouter(prefix="/chat", dependencies=[Depends(_require_chat_auth)])


@dataclass
class ChatSession:
    """Per-session chat state."""

    # A ChatHistory, not a list: it redacts every row on the way in, so every
    # `session.history.append(...)` anywhere in the engine is covered without
    # being edited — including the Telegram ones round 1 missed, and the next
    # channel's. See robothor/engine/chat_history.py.
    history: list[dict[str, Any]] = field(default_factory=ChatHistory)

    def __setattr__(self, name: str, value: Any) -> None:
        """Assigning ``history`` wraps it; a plain list cannot be stored here.

        Round 2 gave the container the property and then two restore paths
        assigned straight over it — `chat._restore_sessions` at daemon startup
        and telegram.py's own — so after every deploy every live session,
        including the one the operator pastes tokens into, held a plain list
        again and `/chat/history` served the raw value out of RAM.

        The fix is not a third call site. A list of blessed call sites is what
        produced that finding twice, so the type is enforced where the
        assignment happens: any module, any path, including ones not written
        yet. The rows already in the assigned list are redacted too — they are
        the ones that came out of the store carrying the credential.
        """
        if name == "history":
            value = as_history(value)
        super().__setattr__(name, value)

    active_task: asyncio.Task[Any] | None = None
    active_request_id: str | None = None
    model_override: str | None = None
    plan_mode: bool = False
    active_plan: PlanState | None = None
    active_deep: DeepRunState | None = None
    last_used: float = field(default_factory=time.monotonic)


# In-memory session cache over the PostgreSQL store (see module docstring and
# robothor/engine/chat_session_cache.py for the policy).
#: Cap on cached sessions. Live work is never dropped to honour it.
MAX_SESSIONS = 500
#: Idle age after which ``evict_idle_sessions`` drops a session (matches the
#: store's own 7-day session TTL).
SESSION_IDLE_TTL_S = 7 * 24 * 3600


def _load_for_cache(session_key: str) -> dict[str, Any]:
    tenant = _config.tenant_id if _config is not None else DEFAULT_TENANT
    return load_session(session_key, limit=MAX_HISTORY, tenant_id=tenant)


_cache = SessionCache(
    max_sessions=MAX_SESSIONS,
    idle_ttl_s=SESSION_IDLE_TTL_S,
    is_pinned=lambda key: key == get_main_session_key(),
    loader=_load_for_cache,
)
_sessions: dict[str, ChatSession] = _cache.sessions
_evicted: set[str] = _cache.evicted


def evict_idle_sessions(*, ttl_s: float | None = None, now: float | None = None) -> int:
    """Drop every evictable session idle for longer than *ttl_s*. Returns the count."""
    return _cache.evict_idle(ttl_s=ttl_s, now=now)


def _get_session(session_key: str) -> ChatSession:
    session: ChatSession = _cache.get(session_key, ChatSession)
    return session


def get_shared_session(session_key: str) -> ChatSession:
    """Public accessor — returns (or creates) the ChatSession for *session_key*.

    Used by telegram.py so both channels share one in-memory session.
    """
    return _get_session(session_key)


def session_count() -> int:
    """How many chat sessions this process is holding. Introspection, not control.

    Public so ``channels/webchat.py``'s ``health()`` can report it without
    reaching into ``_sessions`` from another module.
    """
    return len(_sessions)


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
                    approval_request_id=plan_data.get("approval_request_id", ""),
                    rejection_feedback=plan_data.get("rejection_feedback", ""),
                    plan_hash=plan_data.get("plan_hash", ""),
                    task_context=plan_data.get("task_context", {}),
                    creator_sender_info=plan_data.get("creator_sender_info"),
                    deep_plan=plan_data.get("deep_plan", False),
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
    auth = _auth_context(request)
    identity = _resolve_webchat_identity(auth)

    body = await request.json()
    session_key: str = body.get("session_key", "")
    message: str = body.get("message", "")

    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)

    session_key = _effective_session_key(auth, session_key)
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
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                user_role=auth.role,
                identity=identity,
            )

            final_text = result_text(run)
            # Always record user message in session history
            append_turn(
                session,
                user_message=message,
                assistant_text=final_text or None,
            )

            # Persist to DB (fire-and-forget)
            if final_text and _config:
                asyncio.create_task(
                    save_exchange_async(
                        session_key,
                        message,
                        final_text,
                        channel="webchat",
                        model_override=session.model_override,
                        tenant_id=auth.tenant_id,
                    )
                )

            # Ingest conversation to memory (fire-and-forget)
            if len(session.history) >= 4 and _config:
                from robothor.engine.task_registry import get_task_registry
                from robothor.memory.conversation_ingest import (
                    ingest_conversation_session,
                )

                get_task_registry().spawn(
                    ingest_conversation_session(
                        session_key=session_key,
                        history=list(session.history),
                        agent_id=agent_id,
                        trigger_type="webchat",
                        run_id=run.id,
                        tenant_id=auth.tenant_id,
                    ),
                    name=f"conv-ingest:{session_key}",
                )

            # Signal completion with metadata
            await queue.put(
                {
                    "event": "done",
                    "data": {
                        "text": final_text,
                        "status": run.status.value,
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
            append_turn(
                session,
                user_message=message,
                assistant_text=f"[Internal error — run failed: {e}]",
            )
            if len(session.history) > MAX_HISTORY:
                session.history[:] = session.history[-MAX_HISTORY:]
            await queue.put({"event": "error", "data": {"error": str(e)}})
        finally:
            await queue.put(None)  # Sentinel
            if session.active_task is asyncio.current_task():
                session.active_task = None

    # Start agent as background task
    task = start(session, run_agent, auth, session_key, body.get("request_id"))

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
async def chat_history(request: Request, session_key: str = "", limit: int = 50) -> JSONResponse:
    """Return conversation history for a session."""
    auth = _auth_context(request)
    session_key = _effective_session_key(auth, session_key)
    session = _get_session(session_key)
    messages = session.history[-limit:] if limit > 0 else session.history

    from robothor.engine.runtime.chat_control import recovery_scope

    return JSONResponse(
        {
            "sessionKey": session_key,
            "messages": messages,
            "recoveryScope": recovery_scope(auth, session_key),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/inject")
async def chat_inject(request: Request) -> JSONResponse:
    """Add a system message to the session history."""
    auth = _auth_context(request)
    body = await request.json()
    session_key: str = body.get("session_key", "")
    message: str = body.get("message", "")
    label: str = body.get("label", "")

    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)

    session_key = _effective_session_key(auth, session_key)
    session = _get_session(session_key)
    # Operator-supplied text, so it gets the same pass `append_turn` gives a
    # user turn: this row is persisted and replayed for the life of the session.
    from robothor.secrets.redaction import redact as _redact_text

    session.history.append({"role": "system", "content": _redact_text(message)})

    # Persist to DB (fire-and-forget)
    if _config:
        asyncio.create_task(
            save_message_async(
                session_key,
                "system",
                message,
                channel="webchat",
                tenant_id=auth.tenant_id,
            )
        )

    logger.debug(
        "Injected system message into %s (label=%s)",
        sanitize_log(session_key),
        sanitize_log(label),
    )
    return JSONResponse({"ok": True})


@router.get("/outcome")
async def chat_outcome(
    request: Request, background_tasks: BackgroundTasks, request_id: str, session_key: str = ""
) -> JSONResponse:
    """Read only the authenticated caller's original request record."""
    from robothor.engine.chat_recovery import read_outcome

    auth = _auth_context(request)
    key = _effective_session_key(auth, session_key)
    result = await asyncio.to_thread(read_outcome, auth, key, request_id)
    from robothor.engine.chat_plan_recovery import attach_plan
    from robothor.engine.runtime.chat_control import request_key

    await attach_plan(result, _sessions.get(key), request_key(auth, key, request_id), auth, key)
    if result.get("terminal") and result.get("reconciliation_pending"):
        from robothor.engine.calendar_reconciliation import reconcile_outcome

        background_tasks.add_task(reconcile_outcome, auth, key, request_id)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/abort")
async def chat_abort(request: Request) -> JSONResponse:
    """Cancel the running response for a session."""
    body = await request.json()
    session_key: str = body.get("session_key", "")

    auth = _auth_context(request)
    session_key = _effective_session_key(auth, session_key)
    session = _get_session(session_key)
    return JSONResponse(await stop(session, auth, session_key, body.get("request_id")))


@router.post("/clear")
async def chat_clear(request: Request) -> JSONResponse:
    """Reset session history."""
    auth = _auth_context(request)
    body = await request.json()
    session_key: str = body.get("session_key", "")

    session_key = _effective_session_key(auth, session_key)
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
                tenant_id=auth.tenant_id,
            )
        )

    return JSONResponse({"ok": True})


@router.get("/export")
async def chat_export(request: Request) -> JSONResponse:
    """Export session as markdown or JSON."""
    session_key = request.query_params.get("session_key", "")
    format_ = request.query_params.get("format", "markdown")

    auth = _auth_context(request)
    session_key = _effective_session_key(auth, session_key)
    session = _get_session(session_key)

    if format_ == "json":
        return JSONResponse(
            {
                "session_key": session_key,
                "message_count": len(session.history),
                "history": session.history,
                "model_override": session.model_override,
            }
        )

    from robothor.engine.export import chat_session_to_markdown

    md = chat_session_to_markdown(session, session_key=session_key)
    return JSONResponse({"markdown": md, "session_key": session_key})


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
    if not output or output.startswith("[PLAN_FAILED]"):
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
        "approval_request_id": plan.approval_request_id,
        "deep_plan": plan.deep_plan,
        "plan_hash": plan.plan_hash,
        "task_context": plan.task_context,
    }


# ─── Plan Mode Endpoints ─────────────────────────────────────────────


@router.post("/plan/start", response_model=None)
async def plan_start(request: Request) -> StreamingResponse | JSONResponse:
    """Start plan mode: run agent with read-only tools, return plan via SSE."""
    if _runner is None or _config is None:
        return JSONResponse(
            {"error": "Chat not initialized", "request_admitted": False}, status_code=503
        )
    auth = _auth_context(request)
    identity = _resolve_webchat_identity(auth)

    body = await request.json()
    session_key: str = body.get("session_key", "")
    message: str = body.get("message", "")
    deep_plan: bool = body.get("deep_plan", False)

    if not message:
        return JSONResponse(
            {"error": "message required", "request_admitted": False}, status_code=400
        )

    session_key = _effective_session_key(auth, session_key)
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
                # Limit history for plan exploration to avoid anchoring on
                # rejected plans. Keep only the last 4 messages for context.
                conversation_history=list(session.history[-4:]) if session.history else None,
                readonly_mode=True,
                deep_plan=deep_plan,
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                user_role=auth.role,
                identity=identity,
            )

            # Extract plan from output
            output = result_text(run)
            plan_text = _extract_plan_text(output) if run.status == RunStatus.COMPLETED else ""

            # Accumulate history so revisions have full context
            append_turn(session, user_message=message, assistant_text=output)
            if output and _config:
                asyncio.create_task(
                    save_exchange_async(
                        session_key,
                        message,
                        output,
                        channel="webchat",
                        model_override=session.model_override,
                        tenant_id=auth.tenant_id,
                    )
                )

            if plan_text:
                import hashlib

                plan = PlanState(
                    plan_id=str(uuid.uuid4()),
                    plan_text=plan_text,
                    original_message=message,
                    status="pending",
                    created_at=datetime.now(UTC).isoformat(),
                    exploration_run_id=run.id,
                    deep_plan=deep_plan,
                    plan_hash=hashlib.sha256(plan_text.encode()).hexdigest()[:16],
                )
                # Publish only after the draft can survive a connection loss.
                await save_plan_state_async(
                    session_key, _plan_to_dict(plan), tenant_id=auth.tenant_id, strict=True
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
                        "text": output,
                        "status": run.status.value,
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
            if session.active_task is asyncio.current_task():
                session.active_task = None

    task = start(session, run_plan_agent, auth, session_key, body.get("request_id"))

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
    auth = _auth_context(request)
    identity = _resolve_webchat_identity(auth)

    body = await request.json()
    session_key: str = body.get("session_key", "")
    plan_id: str = body.get("plan_id", "")

    if not plan_id:
        return JSONResponse({"error": "plan_id required"}, status_code=400)

    session_key = _effective_session_key(auth, session_key)
    session = _get_session(session_key)

    if retry := await approval_retry(auth, session_key, body.get("request_id")):
        return retry
    if refusal := approval_refusal(session, plan_id):
        return refusal

    plan, client_id = await admit_plan(session, auth, session_key, body.get("request_id"))
    if plan is None:
        if retry := await approval_retry(auth, session_key, client_id):
            return retry
        return JSONResponse(
            {
                "error": "That plan was already approved or changed. No new execution was started.",
                "request_admitted": False,
            },
            status_code=409,
        )

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
                    trigger_type=TriggerType.WEBCHAT,
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    user_role=auth.role,
                    identity=identity,
                )

                final_text = result_text(run)

                # Track execution run ID
                plan.execution_run_id = run.id

                # Merge into history
                append_turn(
                    session,
                    user_message=f"[Deep plan executed] {plan.original_message}",
                    assistant_text=final_text,
                )

                # Persist to DB
                if final_text and _config:
                    asyncio.create_task(
                        save_exchange_async(
                            session_key,
                            plan.original_message,
                            final_text,
                            channel="webchat",
                            model_override=session.model_override,
                            tenant_id=auth.tenant_id,
                        )
                    )

                # Retire only this approval; newer work may already be pending.
                await finish_plan(session, plan, auth.tenant_id, session_key)

                # Emit deep result + done
                duration_s = (run.duration_ms or 0) / 1000
                cost_usd = run.total_cost_usd or 0.0

                if run.status == RunStatus.COMPLETED and final_text:
                    await queue.put(
                        {
                            "event": "deep_result",
                            "data": {
                                "response": final_text,
                                "execution_time_s": round(duration_s, 1),
                                "cost_usd": round(cost_usd, 2),
                            },
                        }
                    )
                elif run.status != RunStatus.COMPLETED:
                    await queue.put(
                        {"event": "error", "data": {"error": run.error_message or final_text}}
                    )
                await queue.put(
                    {
                        "event": "done",
                        "data": {
                            "text": final_text,
                            "status": run.status.value,
                            "execution_time_s": round(duration_s, 1),
                            "cost_usd": round(cost_usd, 2),
                            "duration_ms": run.duration_ms,
                        },
                    }
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
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    user_role=auth.role,
                    identity=identity,
                )

                final_text = result_text(run)

                # Track execution run ID
                plan.execution_run_id = run.id

                # Merge execution result back into session history for continuity
                append_turn(
                    session,
                    user_message=f"[Plan executed] {plan.original_message}",
                    assistant_text=final_text,
                )

                # Persist to DB
                if final_text and _config:
                    asyncio.create_task(
                        save_exchange_async(
                            session_key,
                            plan.original_message,
                            final_text,
                            channel="webchat",
                            model_override=session.model_override,
                            tenant_id=auth.tenant_id,
                        )
                    )

                # Retire only this approval; newer work may already be pending.
                await finish_plan(session, plan, auth.tenant_id, session_key)

                await queue.put(
                    {
                        "event": "done",
                        "data": {
                            "text": final_text,
                            "status": run.status.value,
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
            if session.active_task is asyncio.current_task():
                session.active_task = None

    task = start(session, run_approved, auth, session_key, client_id)

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
    auth = _auth_context(request)
    body = await request.json()
    session_key: str = body.get("session_key", "")
    plan_id: str = body.get("plan_id", "")
    feedback: str = body.get("feedback", "")

    if not plan_id:
        return JSONResponse({"error": "plan_id required"}, status_code=400)

    session_key = _effective_session_key(auth, session_key)
    session = _get_session(session_key)

    if not session.active_plan or session.active_plan.plan_id != plan_id:
        return JSONResponse({"error": "No matching pending plan"}, status_code=404)

    session.active_plan.status = "rejected"
    session.active_plan.rejection_feedback = feedback

    # Inject rejection feedback into session so agent can learn
    if feedback:
        from robothor.secrets.redaction import redact as _redact_text

        session.history.append(
            {
                "role": "system",
                "content": _redact_text(
                    f"[PLAN REJECTED] The previous plan was rejected. Feedback: {feedback}"
                ),
            }
        )

    session.active_plan = None

    # Persist cleared state
    if _config:
        asyncio.create_task(clear_plan_state_async(session_key, tenant_id=auth.tenant_id))

    return JSONResponse({"ok": True})


@router.post("/plan/iterate", response_model=None)
async def plan_iterate(request: Request) -> StreamingResponse | JSONResponse:
    """Iterate on a pending plan with feedback — revise without restarting."""
    if _runner is None or _config is None:
        return JSONResponse({"error": "Chat not initialized"}, status_code=503)
    auth = _auth_context(request)
    identity = _resolve_webchat_identity(auth)

    body = await request.json()
    session_key: str = body.get("session_key", "")
    plan_id: str = body.get("plan_id", "")
    feedback: str = body.get("feedback", "")

    if not plan_id or not feedback:
        return JSONResponse({"error": "plan_id and feedback required"}, status_code=400)

    session_key = _effective_session_key(auth, session_key)
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
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                user_role=auth.role,
                identity=identity,
            )

            revised_plan_text = _extract_plan_text(run.output_text or "")

            # Update history
            append_turn(session, user_message=feedback, assistant_text=run.output_text)

            if revised_plan_text:
                plan.plan_text = revised_plan_text

                # Persist updated plan state
                asyncio.create_task(
                    save_plan_state_async(
                        session_key,
                        _plan_to_dict(plan),
                        tenant_id=auth.tenant_id,
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
            if session.active_task is asyncio.current_task():
                session.active_task = None

    task = start(session, run_iteration, auth, session_key, body.get("request_id"))

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
async def plan_status(request: Request, session_key: str = "") -> JSONResponse:
    """Check plan state for a session."""
    auth = _auth_context(request)
    session_key = _effective_session_key(auth, session_key)
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
    auth = _auth_context(request)
    identity = _resolve_webchat_identity(auth)

    body = await request.json()
    session_key: str = body.get("session_key", "")
    query: str = (body.get("query", "") or body.get("message", "")).strip()

    if not query:
        return JSONResponse({"error": "query required"}, status_code=400)

    session_key = _effective_session_key(auth, session_key)
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
                trigger_type=TriggerType.WEBCHAT,
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                user_role=auth.role,
                identity=identity,
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
            append_turn(
                session,
                user_message=f"/deep {query}",
                assistant_text=(
                    deep.response
                    or (f"[Deep reasoning failed: {deep.error}]" if deep.error else None)
                ),
            )

            # Persist to DB
            if run.output_text and _config:
                asyncio.create_task(
                    save_exchange_async(
                        session_key,
                        f"/deep {query}",
                        run.output_text,
                        channel="webchat",
                        model_override=session.model_override,
                        tenant_id=auth.tenant_id,
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
            if session.active_task is asyncio.current_task():
                session.active_task = None
            session.active_deep = None

    task = start(session, run_deep, auth, session_key, body.get("request_id"))

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
async def deep_status(request: Request, session_key: str = "") -> JSONResponse:
    """Check deep reasoning state for a session."""
    auth = _auth_context(request)
    session_key = _effective_session_key(auth, session_key)
    session = _get_session(session_key)

    if session.active_deep:
        return JSONResponse(
            {
                "active": True,
                "deep": _deep_to_dict(session.active_deep),
            }
        )
    return JSONResponse({"active": False, "deep": None})
