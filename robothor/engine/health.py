"""
Health endpoint — lightweight FastAPI app for monitoring.

GET /health returns daemon status, scheduler running, bot connected,
and last run per agent.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from robothor.engine.models import TriggerType

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner

logger = logging.getLogger(__name__)


def create_health_app(
    config: EngineConfig, runner: AgentRunner | None = None, workflow_engine=None
):
    """Create a lightweight FastAPI health app."""
    from fastapi import FastAPI

    app = FastAPI(title="Robothor Agent Engine", docs_url=None, redoc_url=None)

    # Mount chat endpoints when runner is available
    if runner is not None:
        from robothor.engine.chat import init_chat
        from robothor.engine.chat import router as chat_router

        init_chat(runner, config)
        app.include_router(chat_router)

    @app.get("/health")
    async def health():
        """Health check endpoint."""
        # Get schedule summary
        schedules = []
        try:
            from robothor.engine.tracking import list_schedules

            schedules = list_schedules(tenant_id=config.tenant_id)
        except Exception as e:
            logger.warning("Failed to load schedules: %s", e)

        agents = {}
        for s in schedules:
            agents[s["agent_id"]] = {
                "enabled": s.get("enabled"),
                "last_status": s.get("last_status"),
                "last_run_at": str(s.get("last_run_at", "")),
                "last_duration_ms": s.get("last_duration_ms"),
                "consecutive_errors": s.get("consecutive_errors", 0),
            }

        return {
            "status": "healthy",
            "timestamp": datetime.now(UTC).isoformat(),
            "engine_version": "0.1.0",
            "tenant_id": config.tenant_id,
            "bot_configured": bool(config.bot_token),
            "agents": agents,
        }

    @app.get("/runs")
    async def list_recent_runs():
        """List recent agent runs."""
        try:
            from robothor.engine.tracking import list_runs

            runs = list_runs(limit=20, tenant_id=config.tenant_id)
            return {
                "runs": [
                    {
                        "id": r["id"],
                        "agent_id": r["agent_id"],
                        "status": r["status"],
                        "trigger_type": r["trigger_type"],
                        "duration_ms": r.get("duration_ms"),
                        "model_used": r.get("model_used"),
                        "input_tokens": r.get("input_tokens"),
                        "output_tokens": r.get("output_tokens"),
                        "created_at": str(r.get("created_at", "")),
                    }
                    for r in runs
                ]
            }
        except Exception as e:
            return {"error": str(e)}

    @app.get("/costs")
    async def costs(hours: int = 24):
        """Cost tracking — per-agent breakdown over the last N hours."""
        try:
            from robothor.engine.tracking import get_agent_stats, list_schedules

            schedules = list_schedules(tenant_id=config.tenant_id)
            agent_ids = [s["agent_id"] for s in schedules]

            total_cost = 0.0
            total_runs = 0
            breakdown = {}

            for agent_id in agent_ids:
                stats = get_agent_stats(agent_id, hours=hours, tenant_id=config.tenant_id)
                runs = int(stats.get("total_runs", 0) or 0)
                cost = float(stats.get("total_cost_usd", 0) or 0)
                total_runs += runs
                total_cost += cost
                if runs > 0:
                    breakdown[agent_id] = {
                        "runs": runs,
                        "completed": int(stats.get("completed", 0) or 0),
                        "failed": int(stats.get("failed", 0) or 0),
                        "timeouts": int(stats.get("timeouts", 0) or 0),
                        "avg_duration_ms": int(stats.get("avg_duration_ms", 0) or 0),
                        "total_input_tokens": int(stats.get("total_input_tokens", 0) or 0),
                        "total_output_tokens": int(stats.get("total_output_tokens", 0) or 0),
                        "total_cost_usd": round(cost, 6),
                    }

            return {
                "hours": hours,
                "total_runs": total_runs,
                "total_cost_usd": round(total_cost, 6),
                "agents": breakdown,
            }
        except Exception as e:
            return {"error": str(e)}

    # ── Workflow API endpoints ───────────────────────────────────────

    @app.get("/api/workflows")
    async def list_workflows():
        """List loaded workflow definitions."""
        if not workflow_engine:
            return {"workflows": []}
        return {
            "workflows": [
                {
                    "id": wf.id,
                    "name": wf.name,
                    "description": wf.description,
                    "version": wf.version,
                    "steps": len(wf.steps),
                    "triggers": [
                        {
                            "type": t.type,
                            "stream": t.stream,
                            "event_type": t.event_type,
                            "cron": t.cron,
                        }
                        for t in wf.triggers
                    ],
                }
                for wf in workflow_engine.list_workflows()
            ]
        }

    @app.get("/api/workflows/{workflow_id}/runs")
    async def list_workflow_runs(workflow_id: str, limit: int = 20):
        """List runs for a specific workflow."""
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """SELECT id, status, trigger_type, trigger_detail,
                              steps_total, steps_completed, steps_failed, steps_skipped,
                              duration_ms, error_message,
                              started_at, completed_at, created_at
                       FROM workflow_runs
                       WHERE workflow_id = %s AND tenant_id = %s
                       ORDER BY created_at DESC LIMIT %s""",
                    (workflow_id, config.tenant_id, limit),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return {
                    "runs": [
                        {
                            c: str(v) if v is not None else None
                            for c, v in zip(cols, row, strict=False)
                        }
                        for row in rows
                    ]
                }
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/workflows/runs/{run_id}")
    async def get_workflow_run(run_id: str):
        """Get workflow run detail with step results."""
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                # Get run
                cur.execute(
                    """SELECT id, workflow_id, status, trigger_type, trigger_detail,
                              steps_total, steps_completed, steps_failed, steps_skipped,
                              duration_ms, error_message, context,
                              started_at, completed_at
                       FROM workflow_runs WHERE id = %s""",
                    (run_id,),
                )
                run_row = cur.fetchone()
                if not run_row:
                    return {"error": "Run not found"}
                cols = [d[0] for d in cur.description]
                run_data: dict[str, Any] = {
                    c: str(v) if v is not None else None
                    for c, v in zip(cols, run_row, strict=False)
                }

                # Get steps
                cur.execute(
                    """SELECT step_id, step_type, status, agent_id, agent_run_id,
                              tool_name, condition_branch, output_text,
                              error_message, duration_ms, started_at, completed_at
                       FROM workflow_run_steps WHERE run_id = %s
                       ORDER BY created_at""",
                    (run_id,),
                )
                step_rows = cur.fetchall()
                step_cols = [d[0] for d in cur.description]
                run_data["steps"] = [
                    {
                        c: str(v) if v is not None else None
                        for c, v in zip(step_cols, row, strict=False)
                    }
                    for row in step_rows
                ]

                return run_data
        except Exception as e:
            return {"error": str(e)}

    # ── v2 Enhancement endpoints ─────────────────────────────────────

    @app.post("/api/runs/{run_id}/resume")
    async def resume_run(run_id: str):
        """Resume a run from its latest checkpoint."""
        if not runner:
            return {"error": "Runner not available"}
        try:
            from robothor.engine.tracking import get_run

            original = get_run(run_id)
            if not original:
                return {"error": f"Run not found: {run_id}"}

            import asyncio

            asyncio.create_task(
                runner.execute(
                    agent_id=original["agent_id"],
                    message="Resume from checkpoint — continue where you left off.",
                    trigger_type=TriggerType.MANUAL,
                    trigger_detail=f"resume:{run_id}",
                    resume_from_run_id=run_id,
                )
            )
            return {"status": "resuming", "original_run_id": run_id}
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/v2/stats")
    async def v2_stats(hours: int = 24):
        """v2 enhancement stats — guardrail events, budget exhaustions, checkpoints."""
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                # Guardrail events
                cur.execute(
                    """SELECT guardrail_name, action, COUNT(*)
                       FROM agent_guardrail_events
                       WHERE created_at > NOW() - INTERVAL '%s hours'
                       GROUP BY guardrail_name, action
                       ORDER BY count DESC""",
                    (hours,),
                )
                guardrails = [
                    {"guardrail": r[0], "action": r[1], "count": r[2]} for r in cur.fetchall()
                ]

                # Budget exhaustions
                cur.execute(
                    """SELECT agent_id, COUNT(*)
                       FROM agent_runs
                       WHERE budget_exhausted = TRUE
                         AND created_at > NOW() - INTERVAL '%s hours'
                       GROUP BY agent_id""",
                    (hours,),
                )
                budgets = {r[0]: r[1] for r in cur.fetchall()}

                # Checkpoints
                cur.execute(
                    """SELECT COUNT(*) FROM agent_run_checkpoints
                       WHERE created_at > NOW() - INTERVAL '%s hours'""",
                    (hours,),
                )
                checkpoint_count = cur.fetchone()[0]

                return {
                    "hours": hours,
                    "guardrail_events": guardrails,
                    "budget_exhaustions": budgets,
                    "checkpoints_saved": checkpoint_count,
                }
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/workflows/{workflow_id}/execute")
    async def execute_workflow(workflow_id: str):
        """Manually trigger a workflow execution."""
        if not workflow_engine:
            return {"error": "Workflow engine not available"}

        wf = workflow_engine.get_workflow(workflow_id)
        if not wf:
            return {"error": f"Workflow not found: {workflow_id}"}

        # Execute in background
        import asyncio

        asyncio.create_task(
            workflow_engine.execute(
                workflow_id=workflow_id,
                trigger_type="manual",
                trigger_detail="api",
            )
        )
        return {"status": "started", "workflow_id": workflow_id}

    return app


async def serve_health(
    config: EngineConfig, runner: AgentRunner | None = None, workflow_engine=None
) -> None:
    """Start the health endpoint server."""
    import uvicorn

    app = create_health_app(config, runner=runner, workflow_engine=workflow_engine)
    uvi_config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=config.port,
        log_level="warning",
    )
    server = uvicorn.Server(uvi_config)
    await server.serve()
