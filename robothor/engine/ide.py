"""IDE WebSocket integration — JSON-RPC 2.0 protocol for IDE extensions.

Provides a WebSocket endpoint at /ide/ws that IDE extensions (VS Code, JetBrains)
can connect to for real-time agent interaction. Uses JSON-RPC 2.0 with streaming
notifications for incremental output.

Protocol:
    -> {"jsonrpc": "2.0", "id": 1, "method": "chat/send", "params": {"message": "..."}}
    <- {"jsonrpc": "2.0", "id": 1, "result": {"status": "streaming", "session_id": "..."}}
    <- {"jsonrpc": "2.0", "method": "chat/delta", "params": {"text": "..."}}
    <- {"jsonrpc": "2.0", "method": "chat/done", "params": {"output": "...", "status": "completed"}}
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner

logger = logging.getLogger(__name__)

router = APIRouter()

_runner: AgentRunner | None = None
_config: EngineConfig | None = None


def init_ide(runner: AgentRunner, config: EngineConfig) -> None:
    """Initialize IDE module. Called once from health.py."""
    global _runner, _config
    _runner = runner
    _config = config


class IdeSession:
    """State for a single IDE WebSocket connection."""

    def __init__(self, ws: WebSocket, session_id: str) -> None:
        self.ws = ws
        self.session_id = session_id
        self.history: list[dict[str, Any]] = []
        self.active_task: asyncio.Task[Any] | None = None

    async def send_result(self, request_id: str | int, result: Any) -> None:
        """Send JSON-RPC 2.0 success response."""
        await self.ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            }
        )

    async def send_error(self, request_id: str | int | None, code: int, message: str) -> None:
        """Send JSON-RPC 2.0 error response."""
        await self.ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    async def send_notification(self, method: str, params: Any) -> None:
        """Send JSON-RPC 2.0 notification (no id)."""
        await self.ws.send_json(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
        )


@router.websocket("/ide/ws")
async def ide_websocket(ws: WebSocket) -> None:
    """WebSocket endpoint for IDE extensions."""
    await ws.accept()
    session = IdeSession(ws, str(uuid.uuid4()))
    logger.info("IDE session connected: %s", session.session_id)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await session.send_error(None, -32700, "Parse error")
                continue

            method = msg.get("method", "")
            params = msg.get("params", {})
            request_id = msg.get("id")

            if method == "chat/send":
                await _handle_chat_send(session, request_id, params)
            elif method == "chat/abort":
                await _handle_chat_abort(session, request_id)
            elif method == "chat/clear":
                session.history.clear()
                await session.send_result(request_id, {"ok": True})
            elif method == "status/health":
                await _handle_health(session, request_id)
            elif method == "skill/list":
                await _handle_skill_list(session, request_id)
            else:
                await session.send_error(request_id, -32601, f"Method not found: {method}")

    except WebSocketDisconnect:
        logger.info("IDE session disconnected: %s", session.session_id)
        if session.active_task and not session.active_task.done():
            session.active_task.cancel()


async def _handle_chat_send(
    session: IdeSession, request_id: str | int | None, params: dict[str, Any]
) -> None:
    """Handle chat/send — run agent, stream deltas as notifications."""
    if _runner is None or _config is None:
        await session.send_error(request_id, -32603, "Engine not initialized")
        return

    message = params.get("message", "")
    if not message:
        await session.send_error(request_id, -32602, "message required")
        return

    agent_id = params.get("agent_id") or _config.default_chat_agent

    # Acknowledge receipt
    await session.send_result(
        request_id,
        {
            "status": "streaming",
            "session_id": session.session_id,
        },
    )

    from robothor.engine.models import TriggerType

    async def _run() -> None:
        last_len = 0

        async def on_content(cumulative: str) -> None:
            nonlocal last_len
            if len(cumulative) > last_len:
                delta = cumulative[last_len:]
                last_len = len(cumulative)
                await session.send_notification("chat/delta", {"text": delta})

        async def on_tool(event: dict[str, Any]) -> None:
            await session.send_notification("chat/tool", event)

        try:
            run = await _runner.execute(
                agent_id=agent_id,
                message=message,
                trigger_type=TriggerType.IDE,
                on_content=on_content,
                on_tool=on_tool,
                conversation_history=list(session.history) if session.history else None,
                model_override=params.get("model"),
            )

            # Update history
            session.history.append({"role": "user", "content": message})
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})

            await session.send_notification(
                "chat/done",
                {
                    "output": run.output_text,
                    "status": run.status.value,
                    "tokens": {"input": run.input_tokens, "output": run.output_tokens},
                    "cost_usd": run.total_cost_usd,
                    "run_id": run.id,
                },
            )
        except Exception as e:
            logger.error("IDE chat/send error: %s", e, exc_info=True)
            await session.send_notification("chat/error", {"error": str(e)})

    session.active_task = asyncio.create_task(_run())


async def _handle_chat_abort(session: IdeSession, request_id: str | int | None) -> None:
    """Cancel the active task."""
    if session.active_task and not session.active_task.done():
        session.active_task.cancel()
        await session.send_result(request_id, {"aborted": True})
    else:
        await session.send_result(request_id, {"aborted": False, "reason": "no active task"})


async def _handle_health(session: IdeSession, request_id: str | int | None) -> None:
    """Return engine health status."""
    await session.send_result(
        request_id,
        {
            "status": "ok",
            "session_id": session.session_id,
            "history_length": len(session.history),
        },
    )


async def _handle_skill_list(session: IdeSession, request_id: str | int | None) -> None:
    """Return available skills."""
    from robothor.engine.skills import load_skills

    skills = load_skills()
    await session.send_result(
        request_id,
        {"skills": [{"name": s.name, "description": s.description} for s in skills.values()]},
    )
