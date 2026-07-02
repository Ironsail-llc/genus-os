"""Observability tool handlers — agent runs, schedules, stats."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@_handler("list_agent_runs")
async def _list_agent_runs(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tracking import list_runs

    runs = await asyncio.to_thread(
        list_runs,
        agent_id=args.get("agent_id"),
        status=args.get("status"),
        limit=args.get("limit", 20),
        tenant_id=ctx.tenant_id,
    )
    return {
        "runs": [
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "status": r["status"],
                "trigger_type": r.get("trigger_type"),
                "model_used": r.get("model_used"),
                "duration_ms": r.get("duration_ms"),
                "input_tokens": r.get("input_tokens"),
                "output_tokens": r.get("output_tokens"),
                "total_cost_usd": float(r["total_cost_usd"]) if r.get("total_cost_usd") else None,
                "started_at": str(r["started_at"]) if r.get("started_at") else None,
                "completed_at": str(r["completed_at"]) if r.get("completed_at") else None,
                "error_message": r.get("error_message"),
            }
            for r in runs
        ],
        "count": len(runs),
    }


@_handler("get_agent_run")
async def _get_agent_run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tracking import get_run, list_steps

    run = await asyncio.to_thread(get_run, args["run_id"])
    if not run:
        return {"error": "Run not found"}
    steps = await asyncio.to_thread(list_steps, args["run_id"])
    return {
        "run": {
            "id": run["id"],
            "agent_id": run["agent_id"],
            "status": run["status"],
            "trigger_type": run.get("trigger_type"),
            "trigger_detail": run.get("trigger_detail"),
            "model_used": run.get("model_used"),
            "models_attempted": run.get("models_attempted"),
            "duration_ms": run.get("duration_ms"),
            "input_tokens": run.get("input_tokens"),
            "output_tokens": run.get("output_tokens"),
            "total_cost_usd": float(run["total_cost_usd"]) if run.get("total_cost_usd") else None,
            "started_at": str(run["started_at"]) if run.get("started_at") else None,
            "completed_at": str(run["completed_at"]) if run.get("completed_at") else None,
            "error_message": run.get("error_message"),
            "delivery_status": run.get("delivery_status"),
        },
        "steps": [
            {
                "step_number": s["step_number"],
                "step_type": s["step_type"],
                "tool_name": s.get("tool_name"),
                "duration_ms": s.get("duration_ms"),
                "error_message": s.get("error_message"),
            }
            for s in steps
        ],
        "step_count": len(steps),
    }


@_handler("classify_run_failure")
async def _classify_run_failure(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Return ground-truth classification of a run's failure.

    Investigators (main, failure-analyzer, improvement-analyst) should call
    this instead of parsing agent_runs.error_message, because the reaper's
    error_message is a label, not a diagnosis. This tool inspects the
    underlying step history and the daemon's boot timestamp to produce a
    structured diagnosis.
    """
    import os

    from robothor.engine.tracking import get_run, list_steps

    run_id = args.get("run_id")
    if not run_id:
        return {"error": "run_id is required"}

    run = await asyncio.to_thread(get_run, run_id)
    if not run:
        return {"error": f"run not found: {run_id}"}

    steps = await asyncio.to_thread(list_steps, run_id)

    llm_steps = [s for s in steps if s.get("step_type") in ("llm_call", "llm_response")]
    llm_was_called = len(llm_steps) > 0
    last_step = steps[-1] if steps else None

    # Derive category the same way the reaper does (reuse classify_reap_reason
    # so the tool and the reaper can never disagree).
    from robothor.engine.daemon import classify_reap_reason

    daemon_start_ts = os.environ.get("ROBOTHOR_DAEMON_START_TS")
    started_at = run.get("started_at")
    started_iso = (
        started_at.isoformat()  # type: ignore[union-attr]
        if hasattr(started_at, "isoformat")
        else str(started_at or "")
    )
    category, _ = classify_reap_reason(str(run_id), started_iso, daemon_start_ts)

    tokens_used = int(run.get("input_tokens") or 0) + int(run.get("output_tokens") or 0)
    started_before_daemon = bool(daemon_start_ts and started_iso and started_iso < daemon_start_ts)

    return {
        "run_id": str(run_id),
        "agent_id": run.get("agent_id"),
        "status": run.get("status"),
        "category": category,
        "raw_error_message": run.get("error_message"),
        "last_step_type": str(last_step.get("step_type")) if last_step else None,
        "last_step_tool": (last_step or {}).get("tool_name"),
        "last_step_error": (last_step or {}).get("error_message"),
        "llm_was_called": llm_was_called,
        "total_llm_calls": len(llm_steps),
        "total_steps": len(steps),
        "model_used": run.get("model_used"),
        "tokens_used": tokens_used,
        "started_at": started_iso or None,
        "completed_at": str(run["completed_at"]) if run.get("completed_at") else None,
        "daemon_started_at": daemon_start_ts,
        "daemon_restart_in_window": started_before_daemon,
    }


@_handler("list_agent_schedules")
async def _list_agent_schedules(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tracking import list_schedules

    schedules = await asyncio.to_thread(
        list_schedules,
        enabled_only=args.get("enabled_only", True),
        tenant_id=ctx.tenant_id,
    )
    return {
        "schedules": [
            {
                "agent_id": s["agent_id"],
                "enabled": s["enabled"],
                "cron_expr": s.get("cron_expr"),
                "timezone": s.get("timezone"),
                "timeout_seconds": s.get("timeout_seconds"),
                "model_primary": s.get("model_primary"),
                "last_run_at": str(s["last_run_at"]) if s.get("last_run_at") else None,
                "last_status": s.get("last_status"),
                "last_duration_ms": s.get("last_duration_ms"),
                "next_run_at": str(s["next_run_at"]) if s.get("next_run_at") else None,
                "consecutive_errors": s.get("consecutive_errors", 0),
            }
            for s in schedules
        ],
        "count": len(schedules),
    }


@_handler("get_agent_stats")
async def _get_agent_stats(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tracking import get_agent_stats as _get_agent_stats

    stats = await asyncio.to_thread(
        _get_agent_stats,
        agent_id=args["agent_id"],
        hours=args.get("hours", 24),
        tenant_id=ctx.tenant_id,
    )
    return {
        "agent_id": args["agent_id"],
        "hours": args.get("hours", 24),
        "total_runs": stats.get("total_runs", 0),
        "completed": stats.get("completed", 0),
        "failed": stats.get("failed", 0),
        "timeouts": stats.get("timeouts", 0),
        "avg_duration_ms": round(float(stats["avg_duration_ms"]))
        if stats.get("avg_duration_ms")
        else None,
        "total_input_tokens": stats.get("total_input_tokens"),
        "total_output_tokens": stats.get("total_output_tokens"),
        "total_cost_usd": float(stats["total_cost_usd"]) if stats.get("total_cost_usd") else None,
    }


@_handler("buddy_refresh")
async def _buddy_refresh(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Compute and persist today's achievement scores into buddy_stats.

    Scores come from goals.py's compute_achievement_score. Flagging + critique
    now live in the Buddy agent (docs/agents/buddy.yaml), not here.
    """
    from robothor.engine.buddy import BuddyEngine

    result = await asyncio.to_thread(BuddyEngine().refresh_daily)
    return result


@_handler("buddy_review_pass")
async def _buddy_review_pass(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Sample recent runs per agent and write Buddy reviews to agent_reviews.

    One review per sampled run. Biases sampling toward failures, runs with
    error steps, and long-duration runs. Uses Sonnet 4.6 to phrase
    evidence-grounded critiques — LLM cannot invent content.
    """
    from robothor.engine.buddy_critic import run_review_pass

    runs_per_agent = int(args.get("runs_per_agent", 3))
    return await run_review_pass(runs_per_agent=runs_per_agent, tenant_id=ctx.tenant_id)


@_handler("buddy_aggregate_findings")
async def _buddy_aggregate_findings(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Aggregate recent Buddy reviews + goal breaches into self-improve tasks.

    For every (agent, breached_metric) above the severity threshold, create
    one CRM task tagged nightwatch+self-improve+<agent>+<metric>. Dedups
    against open tasks for the same (agent, metric).
    """
    from robothor.engine.buddy_critic import run_aggregation_pass

    window_hours = int(args.get("window_hours", 24))
    return await asyncio.to_thread(
        run_aggregation_pass, window_hours=window_hours, tenant_id=ctx.tenant_id
    )


@_handler("buddy_verify_pass")
async def _buddy_verify_pass(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Grade self-improve tasks: verify fixes stuck, hold-check 7d later.

    For every DONE self-improve task older than 48h, re-computes the metric
    and tags verified_resolved or verify_failed (with escalation:N). At
    escalation:3, sets requires_human=true and stops. Separately re-checks
    verified_resolved tasks 7 days later for the hold-rate guardrail.
    """
    from robothor.engine.buddy_grader import run_verification_pass

    return await asyncio.to_thread(run_verification_pass, tenant_id=ctx.tenant_id)


@_handler("get_fleet_achievement_score")
async def _get_fleet_achievement_score(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Aggregate fleet quality signal for the heartbeat.

    Three numbers — today's fleet average achievement_score, the prior-week
    average, and the buddy-grader 14-day hold rate. Pure read-only SQL
    against tables Buddy already populates daily.

    Designed to give the operator one trustworthy line in the heartbeat
    instead of the per-agent self-rated outcome_assessment, which is
    >95% "successful" across the board and carries no signal.
    """
    from robothor.db.connection import get_connection

    def _query() -> dict[str, Any]:
        with get_connection() as conn, conn.cursor() as cur:
            # Today's fleet average — agents with a goals contract only.
            cur.execute(
                """
                SELECT AVG(achievement_score)::float, COUNT(*)
                FROM agent_buddy_stats
                WHERE stat_date = CURRENT_DATE AND achievement_score IS NOT NULL
                """
            )
            today_avg, today_n = cur.fetchone()

            # Prior-week average (last 7 days, excluding today).
            cur.execute(
                """
                SELECT AVG(achievement_score)::float
                FROM agent_buddy_stats
                WHERE stat_date BETWEEN CURRENT_DATE - INTERVAL '7 days'
                                    AND CURRENT_DATE - INTERVAL '1 day'
                  AND achievement_score IS NOT NULL
                """
            )
            (prior_week_avg,) = cur.fetchone()

            # Hold rate — verified_resolved fixes that held vs failed
            # over the last 14 days. Buddy-grader records the outcome
            # via tags `held_7d=true` / `held_7d=false` on the same task
            # (see robothor/engine/health.py:530 for the canonical source).
            cur.execute(
                """
                SELECT
                    SUM(CASE WHEN 'held_7d=true' = ANY(tags) THEN 1 ELSE 0 END)::int,
                    SUM(CASE WHEN 'held_7d=true' = ANY(tags)
                              OR 'held_7d=false' = ANY(tags) THEN 1 ELSE 0 END)::int
                FROM crm_tasks
                WHERE 'verified_resolved' = ANY(tags)
                  AND created_at > NOW() - INTERVAL '14 days'
                """
            )
            held_true, held_total = cur.fetchone()

            hold_rate = (
                round(100.0 * held_true / held_total, 1)
                if held_total and held_true is not None
                else None
            )
            return {
                "today_score": round(today_avg, 1) if today_avg is not None else None,
                "today_agents_scored": today_n or 0,
                "prior_week_score": round(prior_week_avg, 1)
                if prior_week_avg is not None
                else None,
                "delta_vs_prior_week": (
                    round(today_avg - prior_week_avg, 1)
                    if today_avg is not None and prior_week_avg is not None
                    else None
                ),
                "hold_rate_14d_pct": hold_rate,
                "hold_samples_14d": held_total or 0,
            }

    return await asyncio.to_thread(_query)


@_handler("get_accretion_ledger")
async def _get_accretion_ledger(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Self-improvement health: skill accretion + judge volume + reward-hack divergence.

    One line for the evening summary / heartbeat. The DIVERGENCE list is the
    tripwire: agents whose synthetic benchmark passes but whose judge-measured
    real-outcome score is much lower — i.e. acing the exam while failing in
    reality (Phase 4c).
    """
    import os
    import subprocess
    from pathlib import Path

    from robothor.constants import DEFAULT_TENANT

    tenant_id = getattr(ctx, "tenant_id", None) or DEFAULT_TENANT
    window_days = int(args.get("window_days", 7))
    gap_threshold = float(args.get("gap_threshold", 0.25))

    def _query() -> dict[str, Any]:
        from robothor.db.connection import get_connection
        from robothor.engine.goals import compute_goal_metrics
        from robothor.engine.skills import compute_skill_state, load_skills, read_skill_meta

        ws = os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor"))

        # Skill accretion (file-level; no network).
        skills = load_skills()
        total = len(skills)
        archived = 0
        usage: list[tuple[str, int]] = []
        for name in skills:
            meta = read_skill_meta(name) or {}
            if compute_skill_state(meta) == "archived":
                archived += 1
            usage.append((name, int(meta.get("usage_count", 0))))
        usage.sort(key=lambda x: x[1], reverse=True)
        top_used = [f"{n}({c})" for n, c in usage[:3] if c > 0]

        added = 0
        try:
            out = subprocess.run(
                [
                    "git",
                    "-C",
                    ws,
                    "log",
                    f"--since={window_days} days ago",
                    "--diff-filter=A",
                    "--name-only",
                    "--pretty=format:",
                    "--",
                    "agents/skills/*/SKILL.md",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            added = len([ln for ln in out.stdout.splitlines() if ln.strip().endswith("SKILL.md")])
        except Exception:  # noqa: BLE001
            added = -1  # signal "couldn't read git"

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM agent_reviews WHERE reviewer_type='judge' "
                "AND created_at > NOW() - make_interval(days => %s)",
                (window_days,),
            )
            judge_rows = cur.fetchone()[0]
            cur.execute(
                "SELECT DISTINCT agent_id FROM agent_reviews WHERE reviewer_type='judge' "
                "AND created_at > NOW() - make_interval(days => %s)",
                (window_days,),
            )
            judged_agents = [r[0] for r in cur.fetchall()]

        divergent: list[dict[str, Any]] = []
        for agent_id in judged_agents:
            m = compute_goal_metrics(agent_id, window_days=window_days, tenant_id=tenant_id)
            ja, bp = m.get("goal_achievement"), m.get("benchmark_pass_rate")
            if ja is not None and bp is not None and (bp - ja) >= gap_threshold:
                divergent.append(
                    {
                        "agent_id": agent_id,
                        "benchmark": round(bp, 2),
                        "judge": round(ja, 2),
                        "gap": round(bp - ja, 2),
                    }
                )
        divergent.sort(key=lambda d: d["gap"], reverse=True)

        return {
            "skills_total": total,
            "skills_added_7d": added,
            "skills_archived": archived,
            "top_used": top_used,
            "judge_rows_7d": judge_rows,
            "divergent": divergent,
        }

    return await asyncio.to_thread(_query)


@_handler("list_agent_reviews")
async def _list_agent_reviews(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List recent Buddy reviews for an agent.

    Used by agent-architect (and any caller) to read evidence-grounded
    critiques before planning a fix or dispatching work. Returns rating,
    feedback summary, and action_items — full review fetched via
    `get_agent_review(review_id)`.
    """
    from robothor.db.connection import get_connection

    agent_id = args.get("agent_id")
    limit = int(args.get("limit", 20))
    since_hours = int(args.get("since_hours", 168))  # default 7 days

    def _query() -> dict[str, Any]:
        with get_connection() as conn, conn.cursor() as cur:
            params: list[Any] = [ctx.tenant_id, since_hours]
            agent_clause = ""
            if agent_id:
                agent_clause = "AND agent_id = %s"
                params.append(agent_id)
            params.append(limit)
            cur.execute(
                f"""
                SELECT id, agent_id, run_id, reviewer, reviewer_type, rating,
                       LEFT(feedback, 280) AS feedback_excerpt,
                       array_length(action_items, 1) AS action_items_count,
                       created_at
                FROM agent_reviews
                WHERE tenant_id = %s
                  AND created_at > NOW() - (INTERVAL '1 hour' * %s)
                  {agent_clause}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                params,
            )
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
            for r in rows:
                if r.get("created_at"):
                    r["created_at"] = str(r["created_at"])
                if r.get("run_id"):
                    r["run_id"] = str(r["run_id"])
                if r.get("id"):
                    r["id"] = str(r["id"])
            return {"reviews": rows, "count": len(rows)}

    return await asyncio.to_thread(_query)


@_handler("get_agent_review")
async def _get_agent_review(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Fetch one full Buddy review by id, including full feedback + action items."""
    from robothor.db.connection import get_connection

    review_id = args["review_id"]

    def _query() -> dict[str, Any]:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, agent_id, run_id, reviewer, reviewer_type, rating,
                       categories, feedback, action_items, created_at
                FROM agent_reviews
                WHERE tenant_id = %s AND id = %s
                """,
                (ctx.tenant_id, review_id),
            )
            row = cur.fetchone()
            if not row:
                return {"error": f"Review {review_id} not found"}
            cols = [d.name for d in cur.description]
            r = dict(zip(cols, row, strict=False))
            if r.get("created_at"):
                r["created_at"] = str(r["created_at"])
            if r.get("run_id"):
                r["run_id"] = str(r["run_id"])
            r["id"] = str(r["id"])
            return r

    return await asyncio.to_thread(_query)


@_handler("buddy_audit")
async def _buddy_audit(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Weekly hold-rate audit. Pauses Buddy if fixes aren't sticking.

    Reads crm_tasks tagged verified_resolved over the last 14 days, counts
    held_7d=true vs held_7d=false, and pauses Buddy's cron + alerts main
    if the rate is below 30%. Insufficient samples (<5) means no action.
    """
    from robothor.engine.buddy_auditor import run_audit

    outcome = await asyncio.to_thread(run_audit, tenant_id=ctx.tenant_id)
    return {
        "action": outcome.action,
        "total_verifications": outcome.total_verifications,
        "held_true": outcome.held_true,
        "held_false": outcome.held_false,
        "hold_rate": outcome.hold_rate,
        "threshold": outcome.threshold,
        "message": outcome.message,
    }


@_handler("get_agent_performance_summary")
async def _get_agent_performance_summary(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Per-agent grade-card from the most recent benchmark_results row.

    Source of truth for the morning briefing "Agent Performance" section,
    the Telegram /goals command, and the daily summary script. One row
    per agent with: latest pass_rate, last failing case IDs, run timestamp,
    and trend (vs the same agent's prior run).

    Args (all optional):
      agent_id  — filter to a single agent
      since_hours — exclude rows older than this (default: 48)
    """
    from robothor.db.connection import get_connection

    agent_id = (args.get("agent_id") or "").strip() or None
    since_hours = int(args.get("since_hours", 48))

    def _query() -> dict[str, Any]:
        with get_connection() as conn, conn.cursor() as cur:
            # Latest row per agent within the window. DISTINCT ON does the heavy lifting.
            params: list[Any] = [ctx.tenant_id, since_hours]
            extra_clause = ""
            if agent_id:
                extra_clause = " AND agent_id = %s"
                params.append(agent_id)
            cur.execute(
                f"""
                SELECT DISTINCT ON (agent_id)
                  agent_id, suite_id, run_at, total_cases, passed, failed,
                  pass_rate, category_scores, failures, triggered_by, cost_usd
                FROM benchmark_results
                WHERE tenant_id = %s
                  AND run_at >= NOW() - (%s || ' hours')::interval
                  {extra_clause}
                ORDER BY agent_id, run_at DESC
                """,
                params,
            )
            latest_rows = cur.fetchall()
            colnames = [c.name for c in cur.description] if cur.description else []

            agents: list[dict[str, Any]] = []
            for row in latest_rows:
                latest = dict(zip(colnames, row, strict=False))
                # Find the prior row for trend.
                cur.execute(
                    """
                    SELECT pass_rate
                    FROM benchmark_results
                    WHERE tenant_id = %s
                      AND agent_id = %s
                      AND run_at < %s
                    ORDER BY run_at DESC
                    LIMIT 1
                    """,
                    (ctx.tenant_id, latest["agent_id"], latest["run_at"]),
                )
                prior_row = cur.fetchone()
                prior = float(prior_row[0]) if prior_row else None

                pass_rate = float(latest["pass_rate"])
                delta = round(pass_rate - prior, 4) if prior is not None else None
                trend = (
                    "improving"
                    if delta is not None and delta > 0.02
                    else "declining"
                    if delta is not None and delta < -0.02
                    else "flat"
                )

                # `failures` is JSONB; psycopg2 returns dict/list directly.
                failures = latest.get("failures") or []
                if isinstance(failures, str):
                    import json as _json

                    failures = _json.loads(failures)
                failing_ids = [f.get("case_id") for f in failures if f.get("case_id")]

                agents.append(
                    {
                        "agent_id": latest["agent_id"],
                        "suite_id": latest["suite_id"],
                        "run_at": latest["run_at"].isoformat()
                        if hasattr(latest["run_at"], "isoformat")
                        else str(latest["run_at"]),
                        "total_cases": int(latest["total_cases"]),
                        "passed": int(latest["passed"]),
                        "failed": int(latest["failed"]),
                        "pass_rate": round(pass_rate, 3),
                        "prior_pass_rate": round(prior, 3) if prior is not None else None,
                        "delta": delta,
                        "trend": trend,
                        "failing_case_ids": failing_ids,
                        "category_scores": latest.get("category_scores") or {},
                        "triggered_by": latest.get("triggered_by"),
                        "cost_usd": float(latest["cost_usd"]) if latest.get("cost_usd") else None,
                    }
                )

            agents.sort(key=lambda a: a["pass_rate"])  # worst first
            return {"agents": agents, "count": len(agents)}

    return await asyncio.to_thread(_query)
