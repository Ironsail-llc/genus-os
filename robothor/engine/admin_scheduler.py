"""Make a manifest edit take effect — the engine half.

The engine is the process that owns the APScheduler job set, so it is the only
one that can answer "is this agent actually going to fire". The Helm writes
manifests; it cannot reach into another process's job registry, and until this
route existed it did not even have a way to ask. The result was a whole class of
write that reported success and changed nothing: the first-run wizard's agent
install and the marketplace install both wrote a manifest that sat inert until
somebody restarted the engine.

Two routes, both under ``/api/admin``, which ``engine/auth.py`` requires the
``engine:control`` scope for — reads included, and a route added here inherits
that rather than having to remember it.

Its own module rather than four more closures in ``health.py``, for the reason
``admin_providers`` gives: that file is already the engine's largest, and the
surface that changes what the fleet runs should not be hard to find inside it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def _resolve_scheduler(explicit: Any) -> Any:
    """The live scheduler, however this process happens to hold it.

    Threaded in from ``daemon.main`` in production. The fallback to
    ``daemon._ACTIVE_SCHEDULER`` is not belt-and-braces: ``create_health_app``
    is also built by ``robothor.api`` and by tests, where the health app exists
    without anyone passing a scheduler, and the module handle is the same one
    the SIGHUP plugin-reload path already uses.
    """
    if explicit is not None:
        return explicit
    try:
        from robothor.engine import daemon

        return daemon._ACTIVE_SCHEDULER
    except Exception:  # pragma: no cover — importing the daemon is not required
        logger.debug("No scheduler handle available", exc_info=True)
        return None


def registered_tool_names() -> list[str]:
    """Every tool name the engine will accept in ``tools_allowed``.

    Answered here rather than imported by the bridge: ``ToolRegistry()`` builds
    the MCP definitions, the engine schemas and the plugin schemas, so the
    authoritative list only exists in this process. A validator in the bridge
    with its own copy of the list would drift, and a manifest naming a tool the
    engine does not have is one whose agent silently never sees it.
    """
    from robothor.engine.tools import ToolRegistry

    return sorted(ToolRegistry()._schemas.keys())


def register(app: FastAPI, scheduler: Any = None) -> None:
    """Mount the scheduler admin routes on the engine app."""
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/api/admin", tags=["admin"])

    @router.post("/scheduler/reconcile")
    async def reconcile_scheduler() -> Any:
        """Re-derive the live job set from the manifests on disk.

        Idempotent by construction: the response counts what CHANGED, so a
        caller that reconciles after every write sees empty lists whenever the
        write was a no-op. ``blocked`` is the interesting field — a dirty scan
        adds, replaces and prunes nothing, and the operator needs to be told
        that their new agent did not start because a *different* manifest will
        not parse.

        Answered with counts and not a job dump: the ids are already the
        operator's own agent ids, and everything else APScheduler knows about a
        job (its function, its arguments, its next fire time) is either
        uninteresting or instance data.
        """
        live = _resolve_scheduler(scheduler)
        if live is None:
            return JSONResponse({"error": "scheduler not running"}, status_code=503)
        outcome = await live.reconcile()
        body = outcome.as_dict()
        # One structured line, because "the UI said it reconciled" and "the
        # engine reconciled" have been two different states on this appliance.
        # The bridge's audited() is the record of the WRITE; this is the record
        # of the effect.
        logger.info(
            "event=scheduler.reconcile added=%d replaced=%d refreshed=%d "
            "pruned=%d blocked=%d clean=%s",
            len(body["added"]),
            len(body["replaced"]),
            len(body["refreshed"]),
            len(body["pruned"]),
            len(body["blocked"]),
            body["clean"],
        )
        return body

    @router.get("/tools")
    async def list_tools() -> dict[str, Any]:
        """The tool names a manifest may name. See :func:`registered_tool_names`."""
        names = registered_tool_names()
        return {"tools": names, "count": len(names)}

    app.include_router(router)
