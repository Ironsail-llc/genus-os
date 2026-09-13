"""Answering an in-RAM escalation from outside the chat — the engine half.

A durable approval is a row, and anything with a database connection can settle
it. A permission escalation is not: it is an ``asyncio.Event`` inside THIS
process, with a coroutine parked on it. The bridge runs as a separate process,
so a Helm that "approved" an escalation by writing anywhere would be approving
nothing, and the agent would keep waiting until its timeout denied it.

So the decision crosses the process boundary as a call: the bridge proxies to
this route through ``_engine_client.engine_request``, which is also the only
path in that repo allowed to mint an engine credential. Under ``/api/admin``,
which ``engine/auth.py`` gates on ``engine:control`` by prefix — a read-scoped
dashboard token is the wrong identity for approving a tool call.

Its own module rather than four more closures in ``health.py``, for the reason
``admin_scheduler`` gives: that file is the engine's largest, and the surface
that lets something approve a guardrailed tool call should not be hard to find
inside it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def register(app: FastAPI) -> None:
    """Mount the escalation-answer route on the engine app."""
    from fastapi import APIRouter, Body
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/api/admin", tags=["admin"])

    @router.post("/approvals/escalation/{request_id}")
    async def resolve_escalation(
        request_id: str,
        approved: bool = Body(..., embed=True),
        remember_session: bool = Body(False, embed=True),
    ) -> Any:
        """Settle one pending escalation. 404 when there is nothing to settle.

        The three outcomes are deliberately distinct. 200 means a waiting agent
        was just unblocked. 404 means the prompt is gone — timed out, already
        decided, or from a previous process — and answering 200 to that would
        show the operator an approval the agent never received. 503 means this
        engine has no escalation manager at all, which is a configuration fact
        rather than a fact about this request.

        The body carries the verdict and nothing else: the tool arguments in the
        request are the command line an agent wanted to run, and this response
        reaches a browser.
        """
        from robothor.engine.permission_escalation import get_permission_manager

        mgr = get_permission_manager()
        if mgr is None:
            return JSONResponse(
                {"settled": False, "error": "escalation manager not running"}, status_code=503
            )

        settled = mgr.resolve(
            request_id, approved=bool(approved), remember_session=bool(remember_session)
        )
        if not settled:
            return JSONResponse(
                {"settled": False, "error": "no pending escalation with that id"},
                status_code=404,
            )

        logger.info(
            "event=escalation.resolved approved=%s remember_session=%s",
            bool(approved),
            bool(remember_session),
        )
        return {"settled": True, "approved": bool(approved)}

    app.include_router(router)
