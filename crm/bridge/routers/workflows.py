"""Operator-scoped, read-only workflow accounting, from workflow_runs.

Deliberately bridge-side: the engine's /api/workflows* is not operator-scoped and
the dashboard proxy targets the bridge. Lists workflows that have RUN (distinct
workflow_id in workflow_runs); a defined-but-never-run workflow is not shown, and
the tab states that limitation rather than implying full registry coverage.
"""

from __future__ import annotations

import psycopg2.extras
from fastapi import APIRouter, Request

from robothor.db.connection import get_connection
from routers._operator import require_operator

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

_MAX_LIMIT = 100


def _rows(sql: str, params: tuple) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _iso(row: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


def _list_workflows() -> list[dict]:
    rows = _rows(
        "SELECT workflow_id, COUNT(*) AS runs, MAX(started_at) AS last_run_at, "
        "COUNT(*) FILTER (WHERE status IN ('failed','timeout')) AS failures "
        "FROM workflow_runs GROUP BY workflow_id ORDER BY MAX(started_at) DESC NULLS LAST",
        (),
    )
    out = []
    for r in rows:
        last = _rows(
            "SELECT status FROM workflow_runs WHERE workflow_id = %s "
            "ORDER BY started_at DESC NULLS LAST LIMIT 1",
            (r["workflow_id"],),
        )
        d = _iso(r)
        d["last_status"] = last[0]["status"] if last else None
        out.append(d)
    return out


def _workflow_runs(workflow_id: str, limit: int) -> list[dict]:
    rows = _rows(
        "SELECT id, workflow_id, status, trigger_type, steps_total, steps_completed, "
        "steps_failed, steps_skipped, duration_ms, started_at, completed_at, error_message "
        "FROM workflow_runs WHERE workflow_id = %s "
        "ORDER BY started_at DESC NULLS LAST LIMIT %s",
        (workflow_id, limit),
    )
    return [_iso(r) for r in rows]


@router.get("")
def list_workflows(request: Request) -> list[dict]:
    require_operator(request)
    return _list_workflows()


@router.get("/{workflow_id}/runs")
def workflow_runs(workflow_id: str, request: Request, limit: int = 20) -> list[dict]:
    require_operator(request)
    limit = max(1, min(limit, _MAX_LIMIT))
    return _workflow_runs(workflow_id, limit)
