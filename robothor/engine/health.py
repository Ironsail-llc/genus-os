"""
Health endpoint — lightweight FastAPI app for monitoring.

GET /health returns daemon status, scheduler running, bot connected,
and last run per agent.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner

logger = logging.getLogger(__name__)


def create_health_app(config: EngineConfig, runner: AgentRunner | None = None):
    """Create a lightweight FastAPI health app."""
    from fastapi import FastAPI

    app = FastAPI(title="Robothor Agent Engine", docs_url=None, redoc_url=None)

    # Mount chat endpoints when runner is available
    if runner is not None:
        from robothor.engine.chat import init_chat, router as chat_router
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

    return app


async def serve_health(config: EngineConfig, runner: AgentRunner | None = None) -> None:
    """Start the health endpoint server."""
    import uvicorn

    app = create_health_app(config, runner=runner)
    uvi_config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=config.port,
        log_level="warning",
    )
    server = uvicorn.Server(uvi_config)
    await server.serve()
