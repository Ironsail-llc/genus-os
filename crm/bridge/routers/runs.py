"""Tenant-scoped, read-only run accounting: recent runs, the period summary, and
per-run steps + guardrail blocks + cost, for the ACTING tenant. Every run query
names the tenant as well as reading through the RLS-scoped connection, so
isolation does not depend on row-level security being on. Steps and guardrail
events are reached only through a run the tenant owns."""

from __future__ import annotations

import uuid
from decimal import Decimal

import psycopg2.extras
from deps import get_tenant_id
from fastapi import APIRouter, HTTPException, Query, Request, Response

from robothor.db.connection import get_connection
from robothor.engine.analytics import exclude_benchmark_filter
from routers._operator import require_tenant_admin

router = APIRouter(prefix="/api/runs", tags=["runs"])

_MAX_LIMIT = 200
#: A year, in hours: the longest period the list or summary will look back.
_MAX_SINCE_HOURS = 24 * 366

#: The columns every run row carries, list and detail alike.
#:
#: ``delivered_at``, ``delivery_channel``, ``delivery_mode`` and
#: ``verified_status`` joined the list late. The engine had been writing all
#: four since migration 100, and nothing selected them — so a run could show
#: ``completed`` on screen while its answer never left the box, and the
#: verification verdict that would have said so was a column nobody read.
#: "Ran" and "delivered" and "verified" are three different facts and the
#: listing now carries all three.
_RUN_COLUMNS = (
    "id, tenant_id, agent_id, trigger_type, status, started_at, completed_at, "
    "duration_ms, model_used, input_tokens, output_tokens, total_cost_usd, "
    "error_message, delivery_status, delivered_at, delivery_channel, "
    "delivery_mode, verified_status, outcome_assessment"
)


#: What the detail adds to a run row: what it was asked, why it fired, how it
#: judged itself, which models it tried, and where it sits in a spawn tree.
_RUN_DETAIL_COLUMNS = (
    "output_text, error_traceback, task_text, trigger_detail, outcome_notes, "
    "verification, models_attempted, cache_creation_tokens, cache_read_tokens, "
    "parent_run_id, nesting_depth"
)

#: Per step: the model, tokens and timing, the (already redacted) tool input
#: and the tool output as the engine stored it (capped at write time).
_STEP_COLUMNS = (
    "step_number, step_type, tool_name, model, input_tokens, output_tokens, "
    "cache_creation_tokens, cache_read_tokens, duration_ms, error_message, "
    "tool_input, tool_output, started_at, completed_at"
)

#: ``delivery_status`` values the engine writes when it chose not to send
#: (``robothor/engine/delivery.py``). Quiet by design, never a failure.
_NO_DELIVERY_EXPECTED = (
    "silent",
    "suppressed_sub_agent",
    "suppressed_trivial",
    "suppressed_filtered",
    "no_output",
    "published",
)
_DELIVERED = ("delivered", "sent", "ok", "success")


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


#: One run's delivery category, the same buckets the dashboard reads
#: (``app/src/lib/automations/run-truth.ts``). Built from constants only.
#: The status as both readers compare it: trimmed and lower-cased.
_DS = "lower(btrim(delivery_status))"
_DELIVERY_CATEGORY_SQL = (
    "CASE "
    f"WHEN {_DS} IN ({_sql_list(_DELIVERED)}) THEN 'delivered' "
    f"WHEN {_DS} IN ({_sql_list(_NO_DELIVERY_EXPECTED)}) "
    f"OR {_DS} LIKE 'blocked_by_hook:%%' THEN 'none_expected' "
    f"WHEN {_DS} LIKE 'failed%%' OR {_DS} LIKE 'partial:%%' "
    f"OR {_DS} LIKE 'unknown_mode:%%' THEN 'failed' "
    "WHEN NULLIF(btrim(delivery_status), '') IS NULL "
    "AND status IN ('pending', 'running', 'awaiting_approval') THEN 'pending' "
    "ELSE 'not_recorded' END"
)
_DELIVERY_CATEGORIES = ("delivered", "none_expected", "failed", "pending", "not_recorded")


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


def _filters(
    tenant_id: str,
    status: str | None,
    agent: str | None,
    trigger: str | None,
    since: int | None,
) -> tuple[list[str], list]:
    """The WHERE predicates and their parameters. Values are always parameters.

    The tenant predicate is always first and always present.
    """
    clauses: list[str] = ["tenant_id = %s"]
    params: list = [tenant_id]
    for column, value in (("status", status), ("agent_id", agent), ("trigger_type", trigger)):
        if value:
            clauses.append(f"{column} = %s")
            params.append(value)
    if since is not None:
        clauses.append("started_at >= NOW() - make_interval(hours => %s)")
        params.append(since)
    return clauses, params


def _where(clauses: list[str]) -> str:
    return f" WHERE {' AND '.join(clauses)}"


def _list_runs(
    *,
    tenant_id: str,
    status: str | None = None,
    agent: str | None = None,
    trigger: str | None = None,
    since: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    clauses, params = _filters(tenant_id, status, agent, trigger, since)
    sql = (
        f"SELECT {_RUN_COLUMNS} FROM agent_runs{_where(clauses)} "
        "ORDER BY started_at DESC NULLS LAST, id DESC LIMIT %s OFFSET %s"
    )
    rows = _rows(sql, (*params, limit, offset))
    return [_serialize(r) for r in rows]


def _summary(
    *,
    tenant_id: str,
    status: str | None = None,
    agent: str | None = None,
    trigger: str | None = None,
    since: int | None = None,
) -> dict:
    """Counts by status, delivery category and agent, and the period's cost.

    Benchmark traffic is excluded with the analytics module's own predicate,
    so this strip agrees with every other operator-facing number.
    """
    clauses, params = _filters(tenant_id, status, agent, trigger, since)
    clauses.append(exclude_benchmark_filter())
    sql = (
        f"SELECT agent_id, status, {_DELIVERY_CATEGORY_SQL} AS delivery, "
        "COUNT(*) AS runs, COALESCE(SUM(total_cost_usd), 0) AS cost "
        f"FROM agent_runs{_where(clauses)} GROUP BY 1, 2, 3"
    )
    by_status: dict[str, int] = {}
    by_delivery = dict.fromkeys(_DELIVERY_CATEGORIES, 0)
    by_agent: dict[str, dict] = {}
    total = 0
    total_cost = 0.0
    for row in _rows(sql, tuple(params)):
        n = int(row.get("runs") or 0)
        cost = float(row.get("cost") or 0)
        run_status = row.get("status") or "unknown"
        delivery = row.get("delivery") or "not_recorded"
        agent_id = row.get("agent_id") or "unknown"
        total += n
        total_cost += cost
        by_status[run_status] = by_status.get(run_status, 0) + n
        by_delivery[delivery] = by_delivery.get(delivery, 0) + n
        entry = by_agent.setdefault(
            agent_id, {"agent_id": agent_id, "runs": 0, "failed": 0, "cost_usd": 0.0}
        )
        entry["runs"] += n
        entry["cost_usd"] = round(entry["cost_usd"] + cost, 6)
        if run_status in ("failed", "timeout"):
            entry["failed"] += n
    return {
        "since_hours": since,
        "total": total,
        "by_status": by_status,
        "by_delivery": by_delivery,
        "by_agent": sorted(by_agent.values(), key=lambda a: (-a["runs"], a["agent_id"])),
        "total_cost_usd": round(total_cost, 6),
    }


def _get_run(run_id: str, tenant_id: str) -> dict | None:
    rows = _rows(
        f"SELECT {_RUN_COLUMNS}, {_RUN_DETAIL_COLUMNS} FROM agent_runs "
        "WHERE id = %s AND tenant_id = %s",
        (run_id, tenant_id),
    )
    return _serialize(rows[0]) if rows else None


def _get_steps(run_id: str) -> list[dict]:
    rows = _rows(
        f"SELECT {_STEP_COLUMNS} FROM agent_run_steps WHERE run_id = %s "
        "ORDER BY step_number, started_at",
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


_Since = Query(default=None, ge=1, le=_MAX_SINCE_HOURS, description="period, in hours")


@router.get("")
def list_runs(
    request: Request,
    status: str | None = None,
    agent: str | None = None,
    trigger: str | None = None,
    since: int | None = _Since,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    require_tenant_admin(request)
    return _list_runs(
        tenant_id=get_tenant_id(request),
        status=status,
        agent=agent,
        trigger=trigger,
        since=since,
        limit=max(1, min(limit, _MAX_LIMIT)),
        offset=max(0, offset),
    )


# Declared before ``/{run_id}``: otherwise "summary" is read as a run id.
@router.get("/summary")
def runs_summary(
    request: Request,
    status: str | None = None,
    agent: str | None = None,
    trigger: str | None = None,
    since: int | None = _Since,
) -> dict:
    require_tenant_admin(request)
    return _summary(
        tenant_id=get_tenant_id(request),
        status=status,
        agent=agent,
        trigger=trigger,
        since=since,
    )


@router.get("/{run_id}")
def get_run(run_id: str, request: Request, response: Response) -> dict:
    require_tenant_admin(request)
    # Task text, tool input and output: never kept by a shared cache.
    response.headers["Cache-Control"] = "no-store"
    try:
        uuid.UUID(run_id)
    except ValueError:
        # Not an id the table can hold: an unknown run, not a database error.
        raise HTTPException(
            status_code=404, detail="unknown run", headers={"Cache-Control": "no-store"}
        ) from None
    run = _get_run(run_id, get_tenant_id(request))
    if run is None:
        raise HTTPException(
            status_code=404, detail="unknown run", headers={"Cache-Control": "no-store"}
        )
    return {
        "run": run,
        "steps": _get_steps(run_id),
        "guardrail_events": _get_guardrail_events(run_id),
    }
