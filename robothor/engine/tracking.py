"""
Run tracking DAL for the Agent Engine.

Records agent runs, steps (audit trail), and schedule state in PostgreSQL.
Follows robothor/crm/dal.py patterns: get_connection(), RealDictCursor, tenant_id.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection
from robothor.engine.models import AgentRun, RunStatus, RunStep

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "robothor-primary"

# Max chars for tool output stored in steps (prevent bloat)
MAX_TOOL_OUTPUT_CHARS = 4000


def _truncate_json(data: Any, max_chars: int = MAX_TOOL_OUTPUT_CHARS) -> Any:
    """Truncate JSON-serializable data to max chars."""
    if data is None:
        return None
    text = json.dumps(data, default=str)
    if len(text) <= max_chars:
        return data
    return {"_truncated": True, "preview": text[:max_chars]}


# ─── Runs ─────────────────────────────────────────────────────────────


def create_run(run: AgentRun) -> str:
    """Insert a new agent run. Returns the run ID."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO agent_runs (
                id, tenant_id, agent_id, trigger_type, trigger_detail,
                correlation_id, status, started_at, model_used,
                system_prompt_chars, user_prompt_chars, tools_provided,
                delivery_mode
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                run.id,
                run.tenant_id,
                run.agent_id,
                run.trigger_type.value if hasattr(run.trigger_type, "value") else run.trigger_type,
                run.trigger_detail,
                run.correlation_id,
                run.status.value if hasattr(run.status, "value") else run.status,
                run.started_at,
                run.model_used,
                run.system_prompt_chars,
                run.user_prompt_chars,
                run.tools_provided,
                run.delivery_mode,
            ),
        )
    return run.id


def update_run(
    run_id: str,
    *,
    status: str | None = None,
    completed_at: datetime | None = None,
    duration_ms: int | None = None,
    model_used: str | None = None,
    models_attempted: list[str] | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_cost_usd: float | None = None,
    output_text: str | None = None,
    error_message: str | None = None,
    error_traceback: str | None = None,
    delivery_status: str | None = None,
    delivered_at: datetime | None = None,
    delivery_channel: str | None = None,
    token_budget: int | None = None,
    cost_budget_usd: float | None = None,
    budget_exhausted: bool | None = None,
) -> bool:
    """Update an existing run with new fields."""
    updates: list[str] = []
    values: list[Any] = []

    field_map = {
        "status": status,
        "completed_at": completed_at,
        "duration_ms": duration_ms,
        "model_used": model_used,
        "models_attempted": models_attempted,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_cost_usd": total_cost_usd,
        "output_text": output_text,
        "error_message": error_message,
        "error_traceback": error_traceback,
        "delivery_status": delivery_status,
        "delivered_at": delivered_at,
        "delivery_channel": delivery_channel,
        "token_budget": token_budget,
        "cost_budget_usd": cost_budget_usd,
        "budget_exhausted": budget_exhausted,
    }

    for col, val in field_map.items():
        if val is not None:
            updates.append(f"{col} = %s")
            values.append(val)

    if not updates:
        return True

    values.append(run_id)
    sql = f"UPDATE agent_runs SET {', '.join(updates)} WHERE id = %s"

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, values)
        return cur.rowcount > 0


def get_run(run_id: str) -> dict | None:
    """Get a single run by ID."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM agent_runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_runs(
    agent_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    tenant_id: str = DEFAULT_TENANT,
) -> list[dict]:
    """List runs with optional filters."""
    conditions = ["tenant_id = %s"]
    values: list[Any] = [tenant_id]

    if agent_id:
        conditions.append("agent_id = %s")
        values.append(agent_id)
    if status:
        conditions.append("status = %s")
        values.append(status)

    where = " AND ".join(conditions)
    values.append(limit)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"SELECT * FROM agent_runs WHERE {where} ORDER BY created_at DESC LIMIT %s",
            values,
        )
        return [dict(r) for r in cur.fetchall()]


# ─── Steps ────────────────────────────────────────────────────────────


def create_step(step: RunStep) -> str:
    """Insert a new run step (append-only audit trail). Returns step ID."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO agent_run_steps (
                id, run_id, step_number, step_type,
                tool_name, tool_input, tool_output,
                model, input_tokens, output_tokens,
                started_at, completed_at, duration_ms,
                error_message
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                step.id,
                step.run_id,
                step.step_number,
                step.step_type.value if hasattr(step.step_type, "value") else step.step_type,
                step.tool_name,
                json.dumps(step.tool_input, default=str) if step.tool_input else None,
                json.dumps(_truncate_json(step.tool_output), default=str)
                if step.tool_output
                else None,
                step.model,
                step.input_tokens,
                step.output_tokens,
                step.started_at,
                step.completed_at,
                step.duration_ms,
                step.error_message,
            ),
        )
    return step.id


def list_steps(run_id: str) -> list[dict]:
    """List all steps for a run, ordered by step number."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM agent_run_steps WHERE run_id = %s ORDER BY step_number",
            (run_id,),
        )
        return [dict(r) for r in cur.fetchall()]


# ─── Schedules ────────────────────────────────────────────────────────


def get_schedule(agent_id: str) -> dict | None:
    """Get schedule state for an agent."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM agent_schedules WHERE agent_id = %s", (agent_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def upsert_schedule(
    agent_id: str,
    *,
    tenant_id: str = DEFAULT_TENANT,
    enabled: bool = True,
    cron_expr: str = "",
    timezone: str = "America/Grenada",
    timeout_seconds: int = 600,
    model_primary: str | None = None,
    model_fallbacks: list[str] | None = None,
    delivery_mode: str | None = None,
    delivery_channel: str | None = None,
    delivery_to: str | None = None,
    session_target: str | None = None,
) -> bool:
    """Create or update a schedule entry."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO agent_schedules (
                agent_id, tenant_id, enabled, cron_expr, timezone,
                timeout_seconds, model_primary, model_fallbacks,
                delivery_mode, delivery_channel, delivery_to, session_target
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (agent_id) DO UPDATE SET
                tenant_id = EXCLUDED.tenant_id,
                enabled = EXCLUDED.enabled,
                cron_expr = EXCLUDED.cron_expr,
                timezone = EXCLUDED.timezone,
                timeout_seconds = EXCLUDED.timeout_seconds,
                model_primary = EXCLUDED.model_primary,
                model_fallbacks = EXCLUDED.model_fallbacks,
                delivery_mode = EXCLUDED.delivery_mode,
                delivery_channel = EXCLUDED.delivery_channel,
                delivery_to = EXCLUDED.delivery_to,
                session_target = EXCLUDED.session_target,
                updated_at = NOW()
            """,
            (
                agent_id,
                tenant_id,
                enabled,
                cron_expr,
                timezone,
                timeout_seconds,
                model_primary,
                model_fallbacks,
                delivery_mode,
                delivery_channel,
                delivery_to,
                session_target,
            ),
        )
        return True


def update_schedule_state(
    agent_id: str,
    *,
    last_run_at: datetime | None = None,
    last_run_id: str | None = None,
    last_status: str | None = None,
    last_duration_ms: int | None = None,
    next_run_at: datetime | None = None,
    consecutive_errors: int | None = None,
) -> bool:
    """Update runtime state for a schedule after a run completes."""
    updates: list[str] = ["updated_at = NOW()"]
    values: list[Any] = []

    field_map = {
        "last_run_at": last_run_at,
        "last_run_id": last_run_id,
        "last_status": last_status,
        "last_duration_ms": last_duration_ms,
        "next_run_at": next_run_at,
        "consecutive_errors": consecutive_errors,
    }

    for col, val in field_map.items():
        if val is not None:
            updates.append(f"{col} = %s")
            values.append(val)

    values.append(agent_id)
    sql = f"UPDATE agent_schedules SET {', '.join(updates)} WHERE agent_id = %s"

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, values)
        return cur.rowcount > 0


def list_schedules(
    enabled_only: bool = False,
    tenant_id: str = DEFAULT_TENANT,
) -> list[dict]:
    """List all agent schedules."""
    conditions = ["tenant_id = %s"]
    values: list[Any] = [tenant_id]

    if enabled_only:
        conditions.append("enabled = TRUE")

    where = " AND ".join(conditions)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"SELECT * FROM agent_schedules WHERE {where} ORDER BY agent_id",
            values,
        )
        return [dict(r) for r in cur.fetchall()]


def get_agent_stats(
    agent_id: str,
    hours: int = 24,
    tenant_id: str = DEFAULT_TENANT,
) -> dict:
    """Get aggregated stats for an agent over the last N hours."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT
                COUNT(*) as total_runs,
                COUNT(*) FILTER (WHERE status = 'completed') as completed,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                COUNT(*) FILTER (WHERE status = 'timeout') as timeouts,
                AVG(duration_ms) FILTER (WHERE status = 'completed') as avg_duration_ms,
                SUM(input_tokens) as total_input_tokens,
                SUM(output_tokens) as total_output_tokens,
                SUM(total_cost_usd) as total_cost_usd
            FROM agent_runs
            WHERE agent_id = %s
              AND tenant_id = %s
              AND created_at > NOW() - INTERVAL '%s hours'
            """,
            (agent_id, tenant_id, hours),
        )
        row = cur.fetchone()
        return dict(row) if row else {}
