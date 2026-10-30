"""Tenant-scoped, read-only workflow accounting, from workflow_runs.

Deliberately bridge-side: the engine's /api/workflows* is not operator-scoped and
the dashboard proxy targets the bridge. Lists every workflow that has RUN
(distinct workflow_id in workflow_runs) and, for the root tenant, every workflow
DEFINED in the engine's workflow directory (``docs/workflows/*.yaml``) that has
not, with ``runs: 0``. A defined workflow carries ``source``: ``plugin:<dist>``
when the plugin asset ledger records that a plugin wrote it, else ``workspace``.

Every query names the ACTING tenant as well as reading through the RLS-scoped
connection. The workflow directory the bridge can read is its own host's, which
is the root engine's workspace, so defined-but-never-run workflows are listed
only when the acting tenant is the root tenant; a child tenant's definitions
live on that tenant's engine and are not reachable from here.
"""

from __future__ import annotations

import logging

import psycopg2.extras
from deps import get_tenant_id
from fastapi import APIRouter, Request

from robothor.constants import PLATFORM_TENANT
from robothor.db.connection import get_connection
from routers._operator import require_tenant_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

_MAX_LIMIT = 100


def _rows(sql: str, params: tuple) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _iso(row: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


def _list_workflows(tenant_id: str) -> list[dict]:
    rows = _rows(
        "SELECT workflow_id, COUNT(*) AS runs, MAX(started_at) AS last_run_at, "
        "COUNT(*) FILTER (WHERE status IN ('failed','timeout')) AS failures "
        "FROM workflow_runs WHERE tenant_id = %s "
        "GROUP BY workflow_id ORDER BY MAX(started_at) DESC NULLS LAST",
        (tenant_id,),
    )
    out = []
    for r in rows:
        last = _rows(
            "SELECT status FROM workflow_runs WHERE workflow_id = %s AND tenant_id = %s "
            "ORDER BY started_at DESC NULLS LAST LIMIT 1",
            (r["workflow_id"], tenant_id),
        )
        d = _iso(r)
        d["last_status"] = last[0]["status"] if last else None
        out.append(d)
    return out


def _defined_workflows() -> list[dict]:
    """Every parseable workflow in the engine's workflow directory, with its source."""
    from robothor.engine.config import EngineConfig
    from robothor.plugins.overview import parse_workflow_file, workflow_sources

    config = EngineConfig.from_env()
    if not config.workflow_dir.is_dir():
        return []
    warnings: list[str] = []
    sources = workflow_sources(config.workspace, warnings=warnings)
    # Unreadable ledger: no label rather than a confident wrong one.
    fallback = None if warnings else "workspace"
    if warnings:
        # A fixed sentence: nothing read from the workspace reaches the log.
        logger.warning(
            "the plugin asset ledger (.robothor/plugin-assets.yaml) could not be read; "
            "workflows are listed without a source"
        )
    out = []
    for path in sorted(config.workflow_dir.glob("*.yaml")):
        parsed = parse_workflow_file(path)
        if parsed is None:
            continue
        out.append(
            {
                "workflow_id": parsed["id"],
                "name": parsed["name"],
                "triggers": parsed["triggers"],
                "source": sources.get(path.name, fallback),
            }
        )
    return out


def _merged(tenant_id: str) -> list[dict]:
    """Workflows that ran, most recent first, then defined ones that never ran.

    Definitions are read from this host's workflow directory, which belongs to
    the root engine, so they are merged in for the root tenant only.
    """
    found: list[dict] = []
    if tenant_id == PLATFORM_TENANT:
        try:
            found = _defined_workflows()
        except Exception as exc:  # noqa: BLE001 - run history must still answer
            # A config, environment or directory fault reading the definitions
            # must not take the run history down with it.
            logger.warning("defined workflows could not be read: %s", type(exc).__name__)
            found = []
    defined = {d["workflow_id"]: d for d in found}
    out = []
    for row in _list_workflows(tenant_id):
        known = defined.pop(row["workflow_id"], None)
        out.append(
            {
                **row,
                "name": known["name"] if known else None,
                "triggers": known["triggers"] if known else [],
                "source": known["source"] if known else None,
            }
        )
    for workflow_id in sorted(defined):
        d = defined[workflow_id]
        out.append(
            {
                "workflow_id": workflow_id,
                "runs": 0,
                "last_run_at": None,
                "last_status": None,
                "failures": 0,
                "name": d["name"],
                "triggers": d["triggers"],
                "source": d["source"],
            }
        )
    return out


def _workflow_runs(workflow_id: str, limit: int, tenant_id: str) -> list[dict]:
    rows = _rows(
        "SELECT id, workflow_id, status, trigger_type, steps_total, steps_completed, "
        "steps_failed, steps_skipped, duration_ms, started_at, completed_at, error_message "
        "FROM workflow_runs WHERE workflow_id = %s AND tenant_id = %s "
        "ORDER BY started_at DESC NULLS LAST LIMIT %s",
        (workflow_id, tenant_id, limit),
    )
    return [_iso(r) for r in rows]


@router.get("")
def list_workflows(request: Request) -> list[dict]:
    require_tenant_admin(request)
    return _merged(get_tenant_id(request))


@router.get("/{workflow_id}/runs")
def workflow_runs(workflow_id: str, request: Request, limit: int = 20) -> list[dict]:
    require_tenant_admin(request)
    limit = max(1, min(limit, _MAX_LIMIT))
    return _workflow_runs(workflow_id, limit, get_tenant_id(request))
