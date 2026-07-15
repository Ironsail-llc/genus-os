"""Operator-scoped, read-only run accounting: recent runs, and per-run steps +
guardrail blocks + cost. Reads through the RLS-scoped connection, so the operator
sees the platform tenant's runs only."""

from __future__ import annotations

from decimal import Decimal

import psycopg2.extras
from fastapi import APIRouter, HTTPException, Request

from robothor.db.connection import get_connection
from routers._operator import require_operator

router = APIRouter(prefix="/api/runs", tags=["runs"])

_MAX_LIMIT = 200

_RUN_COLUMNS = (
    "id, tenant_id, agent_id, trigger_type, status, started_at, completed_at, "
    "duration_ms, model_used, input_tokens, output_tokens, total_cost_usd, "
    "error_message, delivery_status, outcome_assessment"
)


def _rows(sql: str, params: tuple) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _serialize(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out


def _list_runs(agent: str | None, limit: int) -> list[dict]:
    if agent:
        sql = (
            f"SELECT {_RUN_COLUMNS} FROM agent_runs WHERE agent_id = %s "
            "ORDER BY started_at DESC NULLS LAST LIMIT %s"
        )
        rows = _rows(sql, (agent, limit))
    else:
        sql = f"SELECT {_RUN_COLUMNS} FROM agent_runs ORDER BY started_at DESC NULLS LAST LIMIT %s"
        rows = _rows(sql, (limit,))
    return [_serialize(r) for r in rows]


def _get_run(run_id: str) -> dict | None:
    rows = _rows(
        f"SELECT {_RUN_COLUMNS}, output_text, error_traceback FROM agent_runs WHERE id = %s",
        (run_id,),
    )
    return _serialize(rows[0]) if rows else None


def _get_steps(run_id: str) -> list[dict]:
    rows = _rows(
        "SELECT step_number, step_type, tool_name, model, input_tokens, output_tokens, "
        "duration_ms, error_message FROM agent_run_steps WHERE run_id = %s "
        "ORDER BY step_number",
        (run_id,),
    )
    return [_serialize(r) for r in rows]


def _get_guardrail_events(run_id: str) -> list[dict]:
    rows = _rows(
        "SELECT step_number, guardrail_name, action, tool_name, reason, mode, created_at "
        "FROM agent_guardrail_events WHERE run_id = %s ORDER BY step_number",
        (run_id,),
    )
    return [_serialize(r) for r in rows]


@router.get("")
def list_runs(request: Request, agent: str | None = None, limit: int = 50) -> list[dict]:
    require_operator(request)
    limit = max(1, min(limit, _MAX_LIMIT))
    return _list_runs(agent, limit)


@router.get("/{run_id}")
def get_run(run_id: str, request: Request) -> dict:
    require_operator(request)
    run = _get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return {
        "run": run,
        "steps": _get_steps(run_id),
        "guardrail_events": _get_guardrail_events(run_id),
    }
