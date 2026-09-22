"""Bind browser requests and stops to authenticated session ownership."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

import asyncio
import json
from contextvars import copy_context
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import HTTPException

from robothor.engine.runtime.contracts import ExecutionContext
from robothor.engine.runtime.current import active_context


def request_key(auth: Any, session_key: str, client_id: Any) -> str:
    try:
        identifier = str(UUID(str(client_id))) if client_id else str(uuid4())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="request_id must be a UUID") from exc
    return str(
        uuid5(NAMESPACE_URL, f"webchat:{auth.tenant_id}:{auth.user_id}:{session_key}:{identifier}")
    )


def recovery_scope(auth: Any, session_key: str) -> str:
    """Opaque browser storage namespace, never an authorization credential."""
    identity = json.dumps(["webchat-recovery", auth.tenant_id, auth.user_id, session_key])
    return str(uuid5(NAMESPACE_URL, identity))


def start(
    session: Any,
    factory: Callable[[], Coroutine[Any, Any, Any]],
    auth: Any,
    session_key: str,
    client_id: Any = None,
) -> asyncio.Task[Any]:
    identifier = request_key(auth, session_key, client_id)
    context = copy_context()
    inherited = active_context.get()
    deadline = (
        inherited.deadline
        if inherited
        and (inherited.tenant_id, inherited.principal_id) == (auth.tenant_id, auth.user_id)
        else None
    )
    context.run(
        active_context.set,
        ExecutionContext(auth.tenant_id, auth.user_id, identifier, deadline=deadline),
    )
    task = asyncio.create_task(factory(), context=context)
    session.active_request_id = identifier
    session.active_task = task
    return task


async def stop(session: Any, auth: Any, session_key: str, client_id: Any = None) -> dict[str, Any]:
    from robothor.engine.runtime.controls import issue_request

    identifier = (
        request_key(auth, session_key, client_id) if client_id else session.active_request_id
    )
    target = session.active_task if identifier == session.active_request_id else None
    if identifier:
        # Commit before acknowledging or cancelling the local task. An early
        # stop still denies provider/tool dispatch if admission finishes later.
        await asyncio.to_thread(issue_request, auth.tenant_id, identifier, "Operator stopped chat")
    active = target is not None and not target.done()
    if target is not None and active:
        target.cancel()
    return {"ok": True, "aborted": active, "durable_stopped": bool(identifier)}
