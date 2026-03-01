"""Health, Audit & Telemetry routes."""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from robothor.audit.logger import query_log, query_telemetry, stats
from robothor.crm.dal import check_health

router = APIRouter(tags=["health", "audit"])


@router.get("/health")
async def health():
    """Check connectivity to all dependent services."""
    from bridge_service import _bridge_config, http_client

    services = {}

    try:
        h = check_health()
        services["crm"] = "ok" if h["status"] == "ok" else f"error:{h.get('error', 'unknown')}"
    except Exception as e:
        services["crm"] = f"error:{e}"

    try:
        r = await http_client.get(f"{_bridge_config['memory_url']}/health")
        services["memory"] = "ok" if r.status_code == 200 else f"error:{r.status_code}"
    except Exception as e:
        services["memory"] = f"error:{e}"

    if _bridge_config.get("impetus_one_token"):
        try:
            r = await http_client.get(f"{_bridge_config['impetus_one_url']}/healthz", timeout=5.0)
            services["impetus_one"] = "ok" if r.status_code == 200 else f"error:{r.status_code}"
        except Exception as e:
            services["impetus_one"] = f"error:{e}"

    all_ok = all(v == "ok" for v in services.values())
    status = "ok" if all_ok else "degraded"
    status_code = 200 if all_ok else 503
    return JSONResponse({"status": status, "services": services}, status_code=status_code)


# ─── Audit Endpoints ─────────────────────────────────────────────────────


@router.get("/api/audit")
async def api_query_audit(
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
async def api_audit_stats():
    return stats()


@router.get("/api/telemetry")
async def api_query_telemetry(
    service: str | None = Query(None),
    metric: str | None = Query(None),
    since: str | None = Query(None),
    limit: int = Query(100),
):
    results = query_telemetry(service=service, metric=metric, since=since, limit=limit)
    return {"data": results, "count": len(results)}
