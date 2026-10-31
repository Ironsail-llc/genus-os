"""Operator-scoped, read-only workflow accounting, from workflow_runs.

A run's detail (``/{workflow_id}/runs/{run_id}``) adds each step's tool name,
input, output and error from workflow_run_steps. Those payloads carry whatever
the tools saw, so the response is marked ``Cache-Control: no-store``.

Deliberately bridge-side: the engine's /api/workflows* is not operator-scoped and
the dashboard proxy targets the bridge. Lists every workflow that has RUN
(distinct workflow_id in workflow_runs) and every workflow DEFINED in the
engine's workflow directory (``docs/workflows/*.yaml``) that has not, with
``runs: 0``. A defined workflow carries ``source``: ``plugin:<dist>`` when the
plugin asset ledger records that a plugin wrote it, else ``workspace``.
"""

from __future__ import annotations

import logging
import uuid

import psycopg2.extras
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from robothor.db.connection import get_connection
from routers._operator import require_operator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

_MAX_LIMIT = 100
_NO_STORE = {"Cache-Control": "no-store"}


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


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


def _merged() -> list[dict]:
    """Workflows that ran, most recent first, then defined ones that never ran."""
    try:
        found = _defined_workflows()
    except Exception as exc:  # noqa: BLE001 - run history must still answer
        # A config, environment or directory fault reading the definitions
        # must not take the run history down with it.
        logger.warning("defined workflows could not be read: %s", type(exc).__name__)
        found = []
    defined = {d["workflow_id"]: d for d in found}
    out = []
    for row in _list_workflows():
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


def _workflow_runs(workflow_id: str, limit: int) -> list[dict]:
    rows = _rows(
        "SELECT id, workflow_id, status, trigger_type, steps_total, steps_completed, "
        "steps_failed, steps_skipped, duration_ms, started_at, completed_at, error_message "
        "FROM workflow_runs WHERE workflow_id = %s "
        "ORDER BY started_at DESC NULLS LAST LIMIT %s",
        (workflow_id, limit),
    )
    return [_iso(r) for r in rows]


#: ``workflow_runs.trigger_type`` of a scheduled plugin job's run.
_PLUGIN_JOB = "plugin_job"


def _workflow_run(workflow_id: str, run_id: str) -> dict | None:
    """One run of one workflow, with each step's tool name, input, output and error.

    A scheduled plugin job records no steps; its detail carries what the job
    returned (``result``, and ``result_truncated`` when the engine capped it).
    A workflow's own run context is never served: it holds every step's raw
    output.
    """
    runs = _rows(
        "SELECT id, workflow_id, status, trigger_type, trigger_detail, steps_total, "
        "steps_completed, steps_failed, steps_skipped, duration_ms, started_at, "
        "completed_at, error_message, "
        "CASE WHEN trigger_type = 'plugin_job' THEN context END AS context "
        "FROM workflow_runs WHERE id = %s AND workflow_id = %s",
        (run_id, workflow_id),
    )
    if not runs:
        return None
    steps = _rows(
        "SELECT step_id, step_type, status, agent_id, agent_run_id, tool_name, "
        "tool_input, tool_output, condition_branch, output_text, error_message, "
        "duration_ms, started_at, completed_at "
        "FROM workflow_run_steps WHERE run_id = %s ORDER BY created_at",
        (run_id,),
    )
    detail = _iso(runs[0])
    detail["id"] = str(detail["id"])
    context = detail.pop("context", None)
    if detail.get("trigger_type") == _PLUGIN_JOB and isinstance(context, dict):
        for key in ("result", "result_truncated"):
            if key in context:
                detail[key] = context[key]
    detail["steps"] = [
        {k: (str(v) if k == "agent_run_id" and v is not None else v) for k, v in _iso(s).items()}
        for s in steps
    ]
    return detail


@router.get("")
def list_workflows(request: Request) -> list[dict]:
    require_operator(request)
    return _merged()


@router.get("/{workflow_id}/runs")
def workflow_runs(workflow_id: str, request: Request, limit: int = 20) -> list[dict]:
    require_operator(request)
    limit = max(1, min(limit, _MAX_LIMIT))
    return _workflow_runs(workflow_id, limit)


@router.get("/{workflow_id}/runs/{run_id}")
def workflow_run(workflow_id: str, run_id: str, request: Request) -> JSONResponse:
    """A run's step detail. Operator-only and never cached: the step inputs and
    outputs carry whatever the tools saw."""
    require_operator(request)
    detail = _workflow_run(workflow_id, run_id) if _is_uuid(run_id) else None
    if detail is None:
        return JSONResponse({"detail": "run not found"}, status_code=404, headers=_NO_STORE)
    return JSONResponse(detail, headers=_NO_STORE)
