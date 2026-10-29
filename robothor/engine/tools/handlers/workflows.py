"""Workflow control tools — list, start and report on workflows from a chat.

The operator asks the agent they are talking to; the agent reaches the
process's ``WorkflowEngine`` through ``workflow_registry``. Run facts come
from ``workflow_runs`` and are always filtered to the caller's tenant.

``run_workflow`` never runs a workflow inside the tool call's own lifetime:
the run is a background task the tool merely waits on for a bounded time, so
a slow workflow cannot blow the agent's budget and an agent turn that ends
early cannot cancel the workflow.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from robothor.engine.workflow_registry import get_workflow_engine

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

#: Longest a single run_workflow call waits for the run to finish.
MAX_WAIT_SECONDS = 120
#: With wait_seconds=0, how long to wait anyway so an instant refusal (already
#: running, identity rejected) is reported instead of a misleading "started".
START_GRACE_SECONDS = 2.0
#: Most runs workflow_runs returns in one call.
MAX_RUNS = 50

_NO_ENGINE = {"error": "workflow_engine_unavailable"}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _trigger_view(trigger: Any) -> dict[str, Any]:
    view: dict[str, Any] = {"type": trigger.type}
    if trigger.type == "cron":
        view["cron"] = trigger.cron
    elif trigger.type == "hook":
        view["stream"] = trigger.stream
        view["event_type"] = trigger.event_type
    return view


def _tenant(ctx: ToolContext) -> str:
    from robothor.constants import DEFAULT_TENANT

    return getattr(ctx, "tenant_id", "") or DEFAULT_TENANT


# ── list_workflows ──────────────────────────────────────────────────────


_RUN_FACTS_SQL = """
    SELECT workflow_id,
           (array_agg(status ORDER BY created_at DESC))[1],
           (array_agg(started_at ORDER BY created_at DESC))[1],
           (array_agg(trigger_type ORDER BY created_at DESC))[1],
           count(*) FILTER (
               WHERE status IN ('failed', 'timeout')
                 AND created_at > now() - interval '7 days'
           )
      FROM workflow_runs
     WHERE tenant_id = %s
     GROUP BY workflow_id
"""


def _run_facts(tenant_id: str) -> dict[str, tuple[Any, ...]]:
    from robothor.engine.tools.dispatch import get_db

    with get_db() as conn, conn.cursor() as cur:
        cur.execute(_RUN_FACTS_SQL, (tenant_id,))
        return {row[0]: row for row in cur.fetchall()}


async def _handle_list_workflows(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Every loaded workflow, its triggers, its last run and recent failures."""
    engine = get_workflow_engine()
    if engine is None:
        return dict(_NO_ENGINE)

    facts = await asyncio.to_thread(_run_facts, _tenant(ctx))
    workflows = []
    for wf in engine.list_workflows():
        row = facts.get(wf.id)
        workflows.append(
            {
                "id": wf.id,
                "name": wf.name,
                "description": wf.description,
                "triggers": [_trigger_view(t) for t in wf.triggers],
                "last_run": (
                    {"status": row[1], "started_at": _iso(row[2]), "trigger_type": row[3]}
                    if row
                    else None
                ),
                "failures_7d": int(row[4]) if row else 0,
            }
        )
    return {"workflows": workflows}


# ── run_workflow ────────────────────────────────────────────────────────


async def _handle_run_workflow(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Start a workflow now; optionally wait (bounded) for it to finish."""
    from robothor.engine.models import RunStatus
    from robothor.engine.task_registry import get_task_registry

    engine = get_workflow_engine()
    if engine is None:
        return dict(_NO_ENGINE)

    workflow_id = str(args.get("workflow_id") or "").strip()
    if not workflow_id or engine.get_workflow(workflow_id) is None:
        return {"error": "unknown_workflow", "workflow_id": workflow_id}

    payload = args.get("payload") or {}
    if not isinstance(payload, dict):
        return {"error": "payload must be an object"}
    try:
        wait_seconds = max(0, min(int(args.get("wait_seconds") or 0), MAX_WAIT_SECONDS))
    except (TypeError, ValueError):
        return {"error": "wait_seconds must be an integer"}

    agent_id = getattr(ctx, "agent_id", "") or "unknown"
    run_id = str(uuid.uuid4())
    task = get_task_registry().spawn(
        engine.execute(
            workflow_id=workflow_id,
            trigger_type="agent",
            trigger_detail=f"agent:{agent_id}:{getattr(ctx, 'run_id', '')}",
            initial_context={"event": {"payload": payload}},
            user_id=getattr(ctx, "user_id", "") or f"service:agent:{agent_id}",
            user_role=getattr(ctx, "user_role", "") or "service",
            run_id=run_id,
        ),
        name=f"workflow:{workflow_id}:{run_id}",
    )

    # asyncio.wait never cancels what it waits on: running out of patience
    # here leaves the run going in the background.
    done, _ = await asyncio.wait({task}, timeout=wait_seconds or START_GRACE_SECONDS)
    if not done:
        return {"status": "started", "run_id": run_id}

    exc = task.exception()
    if exc is not None:
        return {"status": "failed", "run_id": run_id, "error_message": str(exc)}
    run = task.result()
    if run.status == RunStatus.SKIPPED:
        return {"status": "already_running"}
    return {"status": str(run.status), "run_id": run.id, "error_message": run.error_message}


# ── workflow_runs ───────────────────────────────────────────────────────


def _recent_runs(workflow_id: str, tenant_id: str, limit: int) -> list[dict[str, Any]]:
    from robothor.engine.tools.dispatch import get_db

    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, status, trigger_type, trigger_detail,
                      started_at, completed_at, error_message
                 FROM workflow_runs
                WHERE workflow_id = %s AND tenant_id = %s
                ORDER BY created_at DESC LIMIT %s""",
            (workflow_id, tenant_id, limit),
        )
        runs = [
            {
                "run_id": str(r[0]),
                "status": r[1],
                "trigger_type": r[2],
                "trigger_detail": r[3],
                "started_at": _iso(r[4]),
                "completed_at": _iso(r[5]),
                "error_message": r[6],
                "steps": [],
            }
            for r in cur.fetchall()
        ]
        if not runs:
            return runs

        # The run ids above are already tenant-scoped; steps belong to them.
        by_id = {r["run_id"]: r for r in runs}
        cur.execute(
            """SELECT run_id, step_id, status
                 FROM workflow_run_steps
                WHERE run_id::text = ANY(%s)
                ORDER BY created_at""",
            (list(by_id),),
        )
        for run_id, step_id, status in cur.fetchall():
            run = by_id.get(str(run_id))
            if run is not None:
                run["steps"].append({"id": step_id, "status": status})
        return runs


async def _handle_workflow_runs(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Recent runs of one workflow, newest first, with per-step status."""
    workflow_id = str(args.get("workflow_id") or "").strip()
    if not workflow_id:
        return {"error": "workflow_id is required"}
    try:
        limit = max(1, min(int(args.get("limit") or 10), MAX_RUNS))
    except (TypeError, ValueError):
        return {"error": "limit must be an integer"}

    runs = await asyncio.to_thread(_recent_runs, workflow_id, _tenant(ctx), limit)
    return {"workflow_id": workflow_id, "runs": runs}


HANDLERS["list_workflows"] = _handle_list_workflows
HANDLERS["run_workflow"] = _handle_run_workflow
HANDLERS["workflow_runs"] = _handle_workflow_runs
