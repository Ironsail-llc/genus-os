"""Health, Audit & Telemetry routes."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, Request  # noqa: TC002 — FastAPI needs it at runtime
from fastapi.responses import JSONResponse

from robothor import __version__
from robothor.audit.logger import query_log, query_telemetry, stats
from robothor.crm.dal import check_health
from robothor.doctor.context import DoctorContext
from robothor.doctor.runner import DoctorReport, run_sync
from routers._operator import require_operator

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health", "audit"])

#: The per-check budget. The same five seconds the CLI uses, and the same the
#: readiness contract gives its own checks -- a Health view whose numbers were
#: produced under a different time box would not be comparable with either.
DOCTOR_TIMEOUT_S = 5.0


@router.get("/health")
async def health():
    """Check connectivity to all dependent services."""
    from bridge_service import _bridge_config, http_client

    services = {}

    try:
        h = await asyncio.to_thread(check_health)
        services["crm"] = "ok" if h["status"] == "ok" else f"error:{h.get('error', 'unknown')}"
    except Exception as e:
        services["crm"] = f"error:{e}"

    try:
        r = await http_client.get(f"{_bridge_config['memory_url']}/health")
        services["memory"] = "ok" if r.status_code == 200 else f"error:{r.status_code}"
    except Exception as e:
        services["memory"] = f"error:{e}"

    all_ok = all(v == "ok" for v in services.values())
    status = "ok" if all_ok else "degraded"
    status_code = 200 if all_ok else 503
    return JSONResponse({"status": status, "services": services}, status_code=status_code)


@router.get("/live")
@router.get("/liveness")
def liveness():
    """Liveness probe; ``/liveness`` is retained as a compatibility alias."""
    from robothor.health_contract import liveness_response

    return liveness_response("bridge", __version__)


@router.get("/ready")
async def readiness():
    """Readiness probe — checks all dependencies."""
    from fastapi.responses import JSONResponse

    from robothor.health_contract import readiness_response

    async def check_crm():
        from robothor.crm.dal import check_health

        h = await asyncio.to_thread(check_health)
        return "ok" if h["status"] == "ok" else f"error:{h.get('error', 'unknown')}"

    async def check_memory():
        from bridge_service import _bridge_config, http_client

        if http_client is None:
            return "error:http-client-not-started"
        r = await http_client.get(f"{_bridge_config['memory_url']}/ready")
        return "ok" if r.status_code == 200 else f"error:{r.status_code}"

    async def check_sso_secret() -> str:
        # A bridge with no GENUS_BRIDGE_SSO_SECRET refuses every /api/auth/sso
        # exchange, so nobody can sign in. That is not "ready", and before this
        # check it was invisible: the bridge reported ready for eight days
        # while every login 403'd (2026-09-03 boot-order race).
        #
        # Gated on auth_required() inside the helper: a loopback dev bridge
        # that never performs an SSO exchange must not be marked not-ready
        # forever, or a readiness probe would pull it out of its Service.
        from routers.auth import sso_secret_readiness_check

        return sso_secret_readiness_check()

    checks = {"crm": check_crm, "memory": check_memory, "sso_secret": check_sso_secret}
    body, status = await readiness_response("bridge", __version__, checks)
    return JSONResponse(body, status_code=status)


@router.get("/api/doctor")
async def api_doctor(request: Request):
    """The full diagnostic, for the Helm's Health view and the setup wizard.

    Operator-gated by the same primitive as every other Helm surface, and for
    a sharper reason than most: this enumerates what is wrong with the
    appliance, which services it runs, which providers it holds credentials
    for and which migrations it is missing. That is a map for anyone who
    should not have it.

    A REPORT, not a probe. ``/ready`` answers 503 when a dependency is down
    because a load balancer must act on it; this answers 200 with
    ``status: "degraded"``, because a dashboard asking "what is wrong" must not
    have its answer withheld on the grounds that something is. The top-level
    ``status`` and ``checks`` keys deliberately mirror
    :func:`robothor.health_contract.readiness_response` so one component
    renders either payload.

    Never repairs. A GET that could seed a role or apply a migration would
    make a page refresh a write.
    """
    require_operator(request)

    def _run() -> DoctorReport:
        # In a worker thread: the checks are synchronous underneath (psycopg2,
        # urllib, a subprocess) and run_sync opens its own event loop, which it
        # cannot do on the one serving this request.
        return run_sync(DoctorContext(timeout_s=DOCTOR_TIMEOUT_S))

    try:
        report = await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001 - a broken doctor is a report, not a 500
        logger.warning("doctor route failed: %s", type(exc).__name__)
        report = DoctorReport(errored=True, error_detail=f"{type(exc).__name__}: {exc}")
    return JSONResponse(report.as_dict(), status_code=200)


# ─── Audit Endpoints ─────────────────────────────────────────────────────


@router.get("/api/audit")
def api_query_audit(
    event_type: str | None = Query(None),
    category: str | None = Query(None),
    actor: str | None = Query(None),
    target: str | None = Query(None),
    since: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50),
):
    results = query_log(
        limit=limit,
        event_type=event_type,
        category=category,
        actor=actor,
        target=target,
        since=since,
        status=status,
    )
    return {"events": results, "count": len(results)}


@router.get("/api/audit/stats")
def api_audit_stats():
    return stats()


@router.get("/api/telemetry")
def api_query_telemetry(
    service: str | None = Query(None),
    metric: str | None = Query(None),
    since: str | None = Query(None),
    limit: int = Query(100),
):
    results = query_telemetry(service=service, metric=metric, since=since, limit=limit)
    return {"data": results, "count": len(results)}
