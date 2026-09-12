"""Health, Audit & Telemetry routes."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

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

#: A budget for the WHOLE run. The per-check box alone leaves a worst case of
#: 26 checks x 5s, over two minutes, held on one of asyncio's default-executor
#: workers (min(32, cpu+4) of them) -- a handful of concurrent operator
#: requests would starve every other to_thread route in the bridge, and the
#: tunnel in front of it gives up at 100s regardless. Checks the budget does
#: not reach are reported as not run, never as passing.
DOCTOR_TOTAL_TIMEOUT_S = 30.0

#: How long a completed report is reused. This route exists to be polled by the
#: Health view, and a poll that runs a fresh 26-check doctor puts load on the
#: very database it is reporting on -- and, when a dependency is wedged, orphans
#: a worker thread per poll in the process that is the only door to the CRM and
#: the Helm. One report per half-minute is fresh enough for a dashboard and
#: bounds both. The CLI is where an operator gets an answer taken just now.
DOCTOR_CACHE_TTL_S = 30.0

#: Seam for the suite, so a test can move past the window without sleeping.
_now = time.monotonic

#: The last completed report and when it was taken. Errors are deliberately NOT
#: cached: an error is not a report, and keeping one would answer with it for
#: the whole window after the cause was fixed.
_doctor_cache: tuple[float, dict[str, Any]] | None = None

#: Single flight. Five operator tabs refreshing at once must produce ONE doctor
#: run, not five competing for the same connection pool -- each of which would
#: also orphan its own worker if the run is the kind that abandons checks. A
#: request arriving mid-run waits for that run's result rather than starting
#: another.
#:
#: A THREADING lock, not an ``asyncio`` one, and held inside the worker thread
#: rather than across an await. Two reasons. An ``asyncio.Lock`` binds to the
#: first event loop that awaits it and raises on any other, so a module-level
#: one is wrong for any host that serves requests from more than one loop --
#: which the test client does, one per request. And the thing being serialised
#: is a blocking run on a worker thread, so the wait belongs there: the event
#: loop is never held, and a waiting request costs one idle worker for the
#: length of a run that is itself bounded at DOCTOR_TOTAL_TIMEOUT_S.
_doctor_guard = threading.Lock()


def reset_doctor_cache() -> None:
    """Forget the memoised report. For tests, and after a deliberate reload."""
    global _doctor_cache  # noqa: PLW0603
    _doctor_cache = None


def _cached_or_run() -> dict[str, Any]:
    """The memoised report, running the doctor only if nobody else just did.

    Runs on a worker thread (``asyncio.to_thread``) because everything under a
    check is synchronous -- psycopg2, urllib -- and ``run_sync`` opens its own
    event loop, which it cannot do on the one serving the request.

    The freshness check is INSIDE the lock on purpose: a caller that waited for
    an in-flight run must then find that run's result rather than start its own.
    Checking first and locking second is the version that lets five concurrent
    polls become five doctors.
    """
    global _doctor_cache  # noqa: PLW0603

    with _doctor_guard:
        cached = _doctor_cache
        if cached is not None and _now() - cached[0] < DOCTOR_CACHE_TTL_S:
            return cached[1]
        report = run_sync(
            DoctorContext(
                timeout_s=DOCTOR_TIMEOUT_S,
                total_timeout_s=DOCTOR_TOTAL_TIMEOUT_S,
                offline=True,
            )
        )
        payload = report.as_dict()
        _doctor_cache = (_now(), payload)
        return payload


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

    Runs OFFLINE. This is what a Health panel polls, and the online doctor
    makes a real completion through the fleet's default model, a getMe against
    Telegram and a fork of the host script: two operator tabs refreshing every
    30 seconds would be 5,760 paid provider calls a day caused by a dashboard.
    The checks that cost money, leave the box or fork a process report
    themselves skipped with the reason, which is honest; the CLI is where a
    full run belongs, because someone asked for it.

    MEMOISED, behind a single-flight lock. Both halves are needed and they
    bound different things. The memo bounds how often a run happens at all; the
    lock bounds how many happen at once, so concurrent polls share one run
    instead of each starting a doctor -- and, when a dependency is wedged, each
    orphaning its own worker thread in the process that is the only door to the
    CRM and the Helm. The report says nothing about when it was taken, so a
    client that needs a fresh answer uses the CLI.
    """
    require_operator(request)

    try:
        payload = await asyncio.to_thread(_cached_or_run)
    except Exception as exc:  # noqa: BLE001 - a broken doctor is a report, not a 500
        logger.warning("doctor route failed: %s", type(exc).__name__)
        # Deliberately not cached: see _doctor_cache.
        errored = DoctorReport(errored=True, error_detail=f"{type(exc).__name__}: {exc}")
        return JSONResponse(errored.as_dict(), status_code=200)
    return JSONResponse(payload, status_code=200)


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
