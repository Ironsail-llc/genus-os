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
    config: EngineConfig, runner: AgentRunner | None = None, workflow_engine: Any = None
) -> Any:
    """Create a lightweight FastAPI health app."""
    from fastapi import FastAPI

    app = FastAPI(title="Genus OS Agent Engine", docs_url=None, redoc_url=None)

    # Mount dashboard endpoints (replaces brain/ Node.js servers)
    from robothor.engine.dashboards import get_dashboard_router, get_public_router

    app.include_router(get_dashboard_router())
    app.include_router(get_public_router())

    # Mount chat endpoints when runner is available
    if runner is not None:
        from robothor.engine.chat import init_chat
        from robothor.engine.chat import router as chat_router

        init_chat(runner, config)
        app.include_router(chat_router)

        # IDE WebSocket integration
        from robothor.engine.ide import init_ide
        from robothor.engine.ide import router as ide_router

        init_ide(runner, config)
        app.include_router(ide_router)

    # Mount webhook ingress
    from robothor.engine.webhooks import get_webhook_router

    app.include_router(get_webhook_router())

    # ── Buddy / KAIROS / Extensions API routes ───────────────────────────

    @app.get("/api/buddy/stats")
    async def buddy_stats() -> dict[str, Any]:
        """Fleet achievement snapshot — aggregated goal satisfaction, not RPG."""
        from robothor.engine.buddy import BuddyEngine

        engine = BuddyEngine()
        fleet = engine.compute_daily_stats()
        current_streak, longest_streak = engine.get_streak()
        return {
            "stat_date": fleet.stat_date.isoformat(),
            "fleet_achievement_score": fleet.fleet_achievement_score,
            "streak": {"current": current_streak, "longest": longest_streak},
            "today": {"tasks": fleet.tasks_completed},
            "agents": [
                {
                    "agent_id": s.agent_id,
                    "achievement_score": s.achievement_score,
                    "rating": s.rating,
                    "satisfied_goals": s.satisfied_goals,
                    "breached_goals": s.breached_goals,
                    "rank": s.rank,
                }
                for s in fleet.per_agent
            ],
        }

    @app.get("/api/buddy/history")
    async def buddy_history(days: int = 7) -> dict[str, Any]:
        """Fleet achievement history for the last N days."""
        days = max(1, min(days, 365))
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT stat_date, tasks_completed, achievement_score,
                       current_streak_days
                FROM buddy_stats
                ORDER BY stat_date DESC
                LIMIT %s
                """,
                (days,),
            )
            rows = cur.fetchall()
        return {
            "days": [
                {
                    "date": str(r[0]),
                    "tasks": r[1],
                    "achievement_score": r[2],
                    "streak": r[3],
                }
                for r in rows
            ]
        }

    @app.get("/api/buddy/agents")
    async def buddy_agents() -> dict[str, Any]:
        """Fleet leaderboard — agents ranked by achievement_score (goals.py)."""
        try:
            from robothor.engine.buddy import BuddyEngine

            scores = BuddyEngine().compute_fleet_scores()
            return {
                "agents": [
                    {
                        "rank": s.rank,
                        "agentId": s.agent_id,
                        "achievementScore": s.achievement_score,
                        "rating": s.rating,
                        "satisfiedGoals": s.satisfied_goals,
                        "breachedGoals": s.breached_goals,
                        "statDate": s.stat_date.isoformat(),
                    }
                    for s in scores
                ]
            }
        except Exception as e:
            logger.warning("Failed to load buddy agents leaderboard: %s", e)
            return {"agents": [], "error": "Failed to load leaderboard"}

    @app.get("/api/buddy/agents/{agent_id}")
    async def buddy_agent_history(agent_id: str, days: int = 14) -> dict[str, Any]:
        """Per-agent achievement history for sparkline/trend charts."""
        days = max(1, min(days, 365))
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT stat_date, achievement_score, tasks_completed
                    FROM agent_buddy_stats
                    WHERE agent_id = %s
                    ORDER BY stat_date DESC
                    LIMIT %s
                    """,
                    (agent_id, days),
                )
                rows = cur.fetchall()
            return {
                "agentId": agent_id,
                "days": [
                    {
                        "date": str(r[0]),
                        "achievementScore": r[1],
                        "tasks": r[2],
                    }
                    for r in rows
                ],
            }
        except Exception as e:
            logger.warning("Failed to load buddy agent history: %s", e)
            return {"agentId": agent_id, "days": [], "error": "Failed to load agent history"}

    @app.get("/api/buddy/ratings")
    async def buddy_ratings() -> dict[str, Any]:
        """Per-agent latest achievement + 7d trend — source of truth for the dashboard."""
        from robothor.db.connection import get_connection

        try:
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT DISTINCT ON (agent_id)
                           agent_id, achievement_score, stat_date
                    FROM agent_buddy_stats
                    WHERE stat_date >= CURRENT_DATE - INTERVAL '1 day'
                      AND achievement_score IS NOT NULL
                    ORDER BY agent_id, stat_date DESC
                    """
                )
                latest = {
                    row[0]: {"score": row[1], "stat_date": str(row[2])} for row in cur.fetchall()
                }

                cur.execute(
                    """
                    SELECT agent_id, stat_date, achievement_score
                    FROM agent_buddy_stats
                    WHERE stat_date >= CURRENT_DATE - INTERVAL '7 days'
                      AND achievement_score IS NOT NULL
                    ORDER BY agent_id, stat_date
                    """
                )
                trend: dict[str, list] = {}
                for agent_id, stat_date, score in cur.fetchall():
                    trend.setdefault(agent_id, []).append({"date": str(stat_date), "score": score})
            return {
                "agents": [
                    {
                        "agentId": aid,
                        "latestScore": v["score"],
                        "statDate": v["stat_date"],
                        "trend": trend.get(aid, []),
                    }
                    for aid, v in sorted(latest.items(), key=lambda kv: -kv[1]["score"])
                ]
            }
        except Exception as e:
            logger.warning("Failed to load buddy ratings: %s", e)
            return {"agents": [], "error": str(e)}

    @app.get("/api/buddy/reviews")
    async def buddy_reviews(limit: int = 50, agent_id: str | None = None) -> dict[str, Any]:
        """Recent Buddy reviews — filter optionally by agent."""
        limit = max(1, min(limit, 500))
        from robothor.db.connection import get_connection

        try:
            with get_connection() as conn:
                cur = conn.cursor()
                if agent_id:
                    cur.execute(
                        """
                        SELECT id, agent_id, run_id, rating, categories, feedback,
                               action_items, created_at
                        FROM agent_reviews
                        WHERE reviewer_type = 'buddy' AND agent_id = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (agent_id, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, agent_id, run_id, rating, categories, feedback,
                               action_items, created_at
                        FROM agent_reviews
                        WHERE reviewer_type = 'buddy'
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                rows = cur.fetchall()
            return {
                "reviews": [
                    {
                        "id": str(r[0]),
                        "agentId": r[1],
                        "runId": str(r[2]) if r[2] else None,
                        "rating": r[3],
                        "categories": r[4],
                        "feedback": r[5],
                        "actionItems": list(r[6]) if r[6] else [],
                        "createdAt": r[7].isoformat() if r[7] else None,
                    }
                    for r in rows
                ]
            }
        except Exception as e:
            logger.warning("Failed to load buddy reviews: %s", e)
            return {"reviews": [], "error": str(e)}

    @app.get("/api/buddy/findings")
    async def buddy_findings() -> dict[str, Any]:
        """Open/in-progress/verifying/resolved/persistent self-improve findings."""
        try:
            from robothor.crm.dal import list_tasks

            open_tasks = list_tasks(
                tags=["self-improve"],
                limit=500,
                exclude_resolved=False,
            )

            def classify(task: dict) -> str | None:
                tags = task.get("tags") or []
                status = (task.get("status") or "").upper()
                # Legacy pre-rebuild tasks (no buddy-baseline marker) are tagged
                # legacy-no-baseline — exclude from the live findings summary.
                if "legacy-no-baseline" in tags:
                    return None
                if task.get("requires_human") or "escalation:3" in tags:
                    return "requires_human"
                if "verified_resolved" in tags:
                    return "resolved"
                if "verify_failed" in tags:
                    return "persistent"
                if status == "DONE":
                    return "verifying"
                if status == "IN_PROGRESS":
                    return "in_progress"
                return "open"

            buckets: dict[str, list] = {
                "open": [],
                "in_progress": [],
                "verifying": [],
                "resolved": [],
                "persistent": [],
                "requires_human": [],
            }
            for task in open_tasks:
                classification = classify(task)
                if classification is None:
                    continue
                buckets.setdefault(classification, []).append(
                    {
                        "id": str(task.get("id")),
                        "title": task.get("title"),
                        "tags": task.get("tags"),
                        "status": task.get("status"),
                        "priority": task.get("priority"),
                        "createdAt": (
                            task["created_at"].isoformat()
                            if isinstance(task.get("created_at"), datetime)
                            else task.get("created_at")
                        ),
                        "assignedToAgent": task.get("assigned_to_agent"),
                    }
                )
            return {
                "summary": {k: len(v) for k, v in buckets.items()},
                "findings": buckets,
            }
        except Exception as e:
            logger.warning("Failed to load buddy findings: %s", e)
            return {"summary": {}, "findings": {}, "error": str(e)}

    @app.get("/api/buddy/verifications")
    async def buddy_verifications() -> dict[str, Any]:
        """List of verified tasks with baseline → current → held_7d for auditor."""
        try:
            import re as _re

            from robothor.crm.dal import list_tasks

            pattern = _re.compile(r"<!--\s*buddy-baseline:\s*(\{.*?\})\s*-->", _re.DOTALL)
            verified = list_tasks(
                tags=["verified_resolved"],
                limit=500,
                exclude_resolved=False,
            )
            out = []
            for task in verified:
                body = task.get("body") or ""
                m = pattern.search(body)
                baseline_blob = None
                if m:
                    try:
                        import json as _json

                        baseline_blob = _json.loads(m.group(1))
                    except Exception:
                        baseline_blob = None
                tags = task.get("tags") or []
                held_7d = None
                if "held_7d=true" in tags:
                    held_7d = True
                elif "held_7d=false" in tags:
                    held_7d = False
                out.append(
                    {
                        "id": str(task.get("id")),
                        "title": task.get("title"),
                        "tags": tags,
                        "verifiedAt": (
                            task["updated_at"].isoformat()
                            if isinstance(task.get("updated_at"), datetime)
                            else task.get("updated_at")
                        ),
                        "baseline": baseline_blob,
                        "held7d": held_7d,
                        "requiresHuman": bool(task.get("requires_human")),
                    }
                )
            return {"verifications": out}
        except Exception as e:
            logger.warning("Failed to load buddy verifications: %s", e)
            return {"verifications": [], "error": str(e)}

    @app.get("/api/buddy/loop-health")
    async def buddy_loop_health(
        open_window_days: int = 30,
        hold_window_days: int = 14,
    ) -> dict[str, Any]:
        """Fleet-level view of the self-improvement loop.

        Returns four metrics derived from `crm_tasks` tags + timestamps
        (no new table, no schema change):

        - `open_breach_count_by_day` — count of non-DONE self-improve tasks
          with created_at on each day, over `open_window_days`. Lets the
          operator see whether the loop's backlog is rising or falling.
        - `time_to_verified_resolved_ms` — p50/p95 latency from
          created_at → verified_at tag. Measures how fast the loop closes
          findings.
        - `escalation_distribution` — count of currently-open tasks at each
          escalation level (0, 1, 2) plus a `requires_human` bucket.
        - `held_7d_rate_rolling_14d` — `held_7d=true / (true + false)` over
          the last `hold_window_days` days (same computation as
          buddy_auditor, exposed as a KPI).
        """
        import re as _re
        from collections import Counter
        from datetime import timedelta
        from statistics import median

        from robothor.crm.dal import list_tasks

        open_window_days = max(1, min(open_window_days, 365))
        hold_window_days = max(1, min(hold_window_days, 365))

        verified_at_re = _re.compile(r"^verified_at:(.+)$")
        escalation_re = _re.compile(r"^escalation:(\d+)$")

        def _parse_verified_at(tags: list[str]) -> datetime | None:
            for t in tags or []:
                m = verified_at_re.match(str(t).strip())
                if not m:
                    continue
                try:
                    parsed = datetime.fromisoformat(m.group(1).strip())
                except ValueError:
                    continue
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed
            return None

        def _as_dt(value: Any) -> datetime | None:
            if isinstance(value, datetime):
                return value if value.tzinfo else value.replace(tzinfo=UTC)
            return None

        now = datetime.now(UTC)
        try:
            tasks = list_tasks(
                tags=["self-improve"],
                limit=2000,
                exclude_resolved=False,
            )
        except Exception as e:
            logger.warning("loop-health: list_tasks failed: %s", e)
            return {"error": str(e)}

        # 1) Open breach count by day
        open_cutoff = now - timedelta(days=open_window_days)
        open_by_day: Counter[str] = Counter()
        for t in tasks:
            if (t.get("status") or "").upper() == "DONE":
                continue
            created = _as_dt(t.get("created_at"))
            if created is None or created < open_cutoff:
                continue
            open_by_day[created.date().isoformat()] += 1
        obd = [{"day": day, "count": open_by_day[day]} for day in sorted(open_by_day)]

        # 2) Verification latency (created_at → verified_at tag)
        latencies_ms: list[float] = []
        for t in tasks:
            tags = t.get("tags") or []
            if "verified_resolved" not in tags:
                continue
            verified_at = _parse_verified_at(tags) or _as_dt(t.get("updated_at"))
            created = _as_dt(t.get("created_at"))
            if verified_at is None or created is None:
                continue
            latencies_ms.append((verified_at - created).total_seconds() * 1000)

        def _percentile(values: list[float], pct: float) -> float | None:
            if not values:
                return None
            values_sorted = sorted(values)
            k = max(
                0, min(len(values_sorted) - 1, int(round((pct / 100) * (len(values_sorted) - 1))))
            )
            return float(values_sorted[k])

        latency = {
            "p50_ms": float(median(latencies_ms)) if latencies_ms else None,
            "p95_ms": _percentile(latencies_ms, 95.0),
            "sample_size": len(latencies_ms),
        }

        # 3) Escalation distribution — current state of non-verified tasks
        esc_counts: dict[str, int] = {"0": 0, "1": 0, "2": 0, "requires_human": 0}
        for t in tasks:
            tags = t.get("tags") or []
            if "verified_resolved" in tags:
                continue  # settled state, not an open escalation
            if t.get("requires_human") or "escalation:3" in tags:
                esc_counts["requires_human"] += 1
                continue
            level = 0
            for tag in tags:
                m = escalation_re.match(str(tag).strip())
                if m:
                    level = max(level, int(m.group(1)))
            if level <= 2:
                esc_counts[str(level)] += 1
            else:
                esc_counts["requires_human"] += 1

        # 4) Rolling held_7d rate
        hold_cutoff = now - timedelta(days=hold_window_days)
        held_true = 0
        held_false = 0
        for t in tasks:
            tags = t.get("tags") or []
            if "verified_resolved" not in tags:
                continue
            updated = _as_dt(t.get("updated_at"))
            if updated is None or updated < hold_cutoff:
                continue
            if "held_7d=true" in tags:
                held_true += 1
            elif "held_7d=false" in tags:
                held_false += 1
        scored = held_true + held_false
        hold_rate = (held_true / scored) if scored > 0 else None

        return {
            "open_breach_count_by_day": obd,
            "time_to_verified_resolved_ms": latency,
            "escalation_distribution": esc_counts,
            "held_7d_rate_rolling_14d": {
                "held_true": held_true,
                "held_false": held_false,
                "rate": hold_rate,
                "window_days": hold_window_days,
            },
            "generated_at": now.isoformat(),
        }

    @app.get("/api/reviews/{agent_id}")
    async def api_get_reviews(agent_id: str, days: int = 30) -> dict[str, Any]:
        """Get reviews for an agent."""
        days = max(1, min(days, 365))
        try:
            from robothor.crm.dal import get_reviews

            reviews = get_reviews(agent_id, days=days)
            return {"agentId": agent_id, "reviews": reviews}
        except Exception as e:
            logger.warning("Failed to load reviews for %s: %s", agent_id, e)
            return {"agentId": agent_id, "reviews": [], "error": str(e)}

    @app.get("/api/reviews/{agent_id}/summary")
    async def api_review_summary(agent_id: str, days: int = 30) -> dict[str, Any]:
        """Get aggregate review summary for an agent."""
        days = max(1, min(days, 365))
        try:
            from robothor.crm.dal import get_review_summary

            summary = get_review_summary(agent_id, days=days)
            return {"agentId": agent_id, **summary}
        except Exception as e:
            logger.warning("Failed to load review summary for %s: %s", agent_id, e)
            return {"agentId": agent_id, "count": 0, "error": str(e)}

    @app.get("/api/kairos/dreams")
    async def kairos_dreams(limit: int = 10) -> dict[str, Any]:
        """Get recent autoDream runs."""
        limit = max(1, min(limit, 1000))
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, mode, started_at, completed_at, duration_ms,
                       facts_consolidated, facts_pruned, insights_discovered,
                       error_message
                FROM autodream_runs
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return {
            "dreams": [
                {
                    "id": str(r[0]),
                    "mode": r[1],
                    "started_at": str(r[2]) if r[2] else None,
                    "completed_at": str(r[3]) if r[3] else None,
                    "duration_ms": r[4],
                    "facts_consolidated": r[5],
                    "facts_pruned": r[6],
                    "insights_discovered": r[7],
                    "error": r[8],
                }
                for r in rows
            ]
        }

    @app.get("/api/extensions")
    async def list_extensions() -> dict[str, Any]:
        """List loaded business adapters / extensions."""
        try:
            from robothor.engine.adapters import get_loaded_adapters

            adapters = get_loaded_adapters()
            return {
                "count": len(adapters),
                "extensions": [
                    {
                        "name": a.name,
                        "transport": a.transport,
                        "version": a.version,
                        "author": a.author,
                        "description": a.description,
                        "agents": a.agents,
                    }
                    for a in adapters
                ],
            }
        except Exception:
            logger.exception("Failed to list extensions")
            return {"error": "Internal server error"}

    @app.post("/api/extensions/reload")
    async def reload_extensions() -> dict[str, Any]:
        """Reload adapters from disk."""
        from robothor.engine.adapters import refresh_adapters

        adapters = refresh_adapters()
        return {"reloaded": True, "count": len(adapters)}

    @app.get("/metrics")
    async def metrics() -> Any:
        """Prometheus metrics endpoint."""
        from fastapi.responses import PlainTextResponse
        from prometheus_client import generate_latest

        return PlainTextResponse(generate_latest(), media_type="text/plain; version=0.0.4")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Health check endpoint."""
        try:
            # Get schedule summary
            schedules = []
            try:
                from robothor.engine.tracking import list_schedules

                schedules = list_schedules(tenant_id=config.tenant_id)
            except Exception:
                logger.warning("Failed to load schedules", exc_info=True)

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
        except Exception:
            logger.exception("Health check failed")
            return {"status": "error", "error": "Internal server error"}

    # Startup state tracking
    _startup_complete = {"ready": False}

    @app.get("/health/startup")
    async def startup() -> Any:
        """Startup probe — returns 503 until initialization is complete."""
        from fastapi.responses import JSONResponse

        if _startup_complete["ready"]:
            return {"status": "started", "service": "engine"}
        return JSONResponse(
            {"status": "starting", "service": "engine"},
            status_code=503,
        )

    @app.get("/liveness")
    async def liveness() -> dict[str, Any]:
        """Liveness probe — always 200 if process is running."""
        try:
            from robothor.health_contract import liveness_response

            return liveness_response("engine", "0.1.0")
        except Exception:
            logger.exception("Liveness check failed")
            return {"status": "error", "error": "Internal server error"}

    @app.get("/ready")
    async def readiness() -> Any:
        """Readiness probe — checks all dependencies."""
        from fastapi.responses import JSONResponse

        from robothor.health_contract import readiness_response

        async def check_db() -> str:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                conn.cursor().execute("SELECT 1")
            return "ok"

        async def check_schedules() -> str:
            from robothor.engine.tracking import list_schedules

            list_schedules(tenant_id=config.tenant_id)
            return "ok"

        try:
            checks: dict[str, Any] = {
                "database": check_db,
                "schedules": check_schedules,
            }
            body, status = await readiness_response("engine", "0.1.0", checks)
            return JSONResponse(body, status_code=status)
        except Exception:
            logger.exception("Readiness check failed")
            return JSONResponse(
                {"status": "error", "error": "Internal server error"}, status_code=500
            )

    @app.on_event("startup")
    async def _mark_startup_complete() -> None:
        """Mark startup as complete once FastAPI is serving."""
        from robothor.db.connection import get_connection

        try:
            with get_connection() as conn:
                conn.cursor().execute("SELECT 1")
            _startup_complete["ready"] = True
            logger.info("Engine startup probe: ready")
        except Exception as e:
            logger.warning("Engine startup probe: DB not ready yet — %s", e)

    @app.get("/runs")
    async def list_recent_runs() -> dict[str, Any]:
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
                        "cache_creation_tokens": r.get("cache_creation_tokens", 0),
                        "cache_read_tokens": r.get("cache_read_tokens", 0),
                        "parent_run_id": str(r["parent_run_id"])
                        if r.get("parent_run_id")
                        else None,
                        "nesting_depth": r.get("nesting_depth", 0),
                        "created_at": str(r.get("created_at", "")),
                    }
                    for r in runs
                ]
            }
        except Exception:
            logger.exception("Failed to list runs")
            return {"error": "Internal server error"}

    @app.get("/api/runs/{run_id}/children")
    async def get_run_children(run_id: str) -> dict[str, Any]:
        """Get direct child runs of a parent run."""
        try:
            from robothor.engine.tracking import get_run_children as _get_children

            children = _get_children(run_id)
            return {
                "parent_run_id": run_id,
                "children": [
                    {
                        "id": c["id"],
                        "agent_id": c["agent_id"],
                        "status": c["status"],
                        "nesting_depth": c.get("nesting_depth", 0),
                        "duration_ms": c.get("duration_ms"),
                        "input_tokens": c.get("input_tokens"),
                        "output_tokens": c.get("output_tokens"),
                        "total_cost_usd": c.get("total_cost_usd"),
                        "started_at": str(c.get("started_at", "")),
                    }
                    for c in children
                ],
            }
        except Exception:
            logger.exception("Failed to get run children")
            return {"error": "Internal server error"}

    @app.get("/api/runs/{run_id}/tree")
    async def get_run_tree(run_id: str) -> dict[str, Any]:
        """Get full execution tree (recursive) for a run."""
        try:
            from robothor.engine.tracking import get_run_tree as _get_tree

            tree = _get_tree(run_id)
            return tree
        except Exception:
            logger.exception("Failed to get run tree")
            return {"error": "Internal server error"}

    @app.get("/api/analytics/threads")
    async def thread_pool_analytics(window_days: int = 7) -> dict[str, Any]:
        """Stage 4 — thread pool metrics (advance rate, question answer rate,
        stall rate, planner override rate)."""
        try:
            from robothor.engine.analytics import thread_pool_metrics

            return thread_pool_metrics(tenant_id=config.tenant_id, window_days=window_days)
        except Exception:
            logger.exception("Failed to compute thread_pool_metrics")
            return {"error": "Internal server error"}

    @app.get("/costs")
    async def costs(hours: int = 24) -> dict[str, Any]:
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
        except Exception:
            logger.exception("Failed to compute costs")
            return {"error": "Internal server error"}

    @app.get("/costs/deep")
    async def costs_deep(hours: int = 24) -> dict[str, Any]:
        """RLM deep reasoning cost tracking — queries runs with deep_reason steps."""
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT
                        COUNT(DISTINCT r.id) AS total_calls,
                        COALESCE(SUM(r.total_cost_usd), 0) AS total_cost_usd,
                        COALESCE(AVG(r.total_cost_usd), 0) AS avg_cost_usd,
                        COALESCE(AVG(r.duration_ms / 1000.0), 0) AS avg_duration_s
                    FROM agent_runs r
                    JOIN agent_run_steps s ON s.run_id = r.id
                    WHERE s.step_type = 'deep_reason'
                      AND r.started_at >= NOW() - make_interval(hours => %s)
                      AND r.tenant_id = %s
                    """,
                    (hours, config.tenant_id),
                )
                row = cur.fetchone()
                if row:
                    return {
                        "hours": hours,
                        "total_calls": row[0],
                        "total_cost_usd": round(float(row[1]), 4),
                        "avg_cost_usd": round(float(row[2]), 4),
                        "avg_duration_s": round(float(row[3]), 1),
                    }
                return {
                    "hours": hours,
                    "total_calls": 0,
                    "total_cost_usd": 0,
                    "avg_cost_usd": 0,
                    "avg_duration_s": 0,
                }
        except Exception:
            logger.exception("LLM cost query failed")
            return {"error": "Internal server error"}

    # ── Workflow API endpoints ───────────────────────────────────────

    @app.get("/api/workflows")
    async def list_workflows() -> dict[str, Any]:
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
    async def list_workflow_runs(workflow_id: str, limit: int = 20) -> dict[str, Any]:
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
        except Exception:
            logger.exception("Failed to list workflow runs")
            return {"error": "Internal server error"}

    @app.get("/api/workflows/runs/{run_id}")
    async def get_workflow_run(run_id: str) -> dict[str, Any]:
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
        except Exception:
            logger.exception("Failed to get workflow run")
            return {"error": "Internal server error"}

    # ── v2 Enhancement endpoints ─────────────────────────────────────

    @app.post("/api/runs/{run_id}/resume")
    async def resume_run(run_id: str) -> dict[str, Any]:
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
        except Exception:
            logger.exception("Failed to resume run")
            return {"error": "Internal server error"}

    @app.get("/api/v2/stats")
    async def v2_stats(hours: int = 24) -> dict[str, Any]:
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
        except Exception:
            logger.exception("Failed to get v2 stats")
            return {"error": "Internal server error"}

    @app.post("/api/workflows/{workflow_id}/execute")
    async def execute_workflow(workflow_id: str) -> dict[str, Any]:
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

    # ── Config + hook introspection endpoints ──────────────────────────

    @app.get("/api/config/explain/{agent_id}")
    async def explain_agent_config(agent_id: str) -> dict[str, Any]:
        import os
        from pathlib import Path

        from robothor.engine.config import explain_config

        _ws = Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")))
        manifest_dir = _ws / "docs" / "agents"
        workspace = _ws

        result = explain_config(agent_id, manifest_dir, workspace=workspace)
        if not result.get("merged"):
            return {"error": f"Agent '{agent_id}' not found"}
        return result

    @app.get("/api/hooks/metrics")
    async def hook_metrics() -> dict[str, Any]:
        from robothor.engine.hook_registry import get_hook_registry

        registry = get_hook_registry()
        if not registry:
            return {"metrics": {}}

        raw = registry.get_metrics()
        # Convert tuple keys to strings for JSON serialization
        return {
            "metrics": {
                f"{handler}:{event}": {
                    "executions": m.executions,
                    "failures": m.failures,
                    "total_duration_ms": round(m.total_duration_ms, 2),
                    "timeouts": m.timeouts,
                }
                for (handler, event), m in raw.items()
            }
        }

    return app


async def serve_health(
    config: EngineConfig, runner: AgentRunner | None = None, workflow_engine: Any = None
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
