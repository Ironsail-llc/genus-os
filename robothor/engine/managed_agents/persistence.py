"""Persist Managed Agents run results to the ``ma_runs`` table.

Uses the existing DB connection pool from ``robothor.db.connection``.
Does NOT modify any existing tables or queries.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.managed_agents.models import MARunResult

logger = logging.getLogger(__name__)


def persist_ma_run(
    result: MARunResult,
    agent_id: str,
    tenant_id: str = "robothor-primary",
    *,
    input_message: str = "",
) -> str | None:
    """Write an MA run result to the ``ma_runs`` table.

    Returns the generated row UUID, or *None* on failure.

    This is a synchronous call (matches the psycopg2 connection pool).
    Callers in async code should wrap with ``run_in_executor``.
    """
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ma_runs (
                        agent_id, tenant_id, ma_session_id, input_message,
                        output_text, input_tokens, output_tokens,
                        total_cost_usd, tool_calls, outcome_result,
                        duration_ms
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        agent_id,
                        tenant_id,
                        result.session_id,
                        input_message,
                        result.output_text or None,
                        result.input_tokens,
                        result.output_tokens,
                        result.total_cost_usd,
                        json.dumps(result.tool_calls) if result.tool_calls else "[]",
                        result.outcome_result,
                        result.duration_ms,
                    ),
                )
                row = cur.fetchone()
                conn.commit()
                row_id = str(row[0]) if row else None
                logger.info(
                    "Persisted MA run %s for agent=%s tenant=%s session=%s",
                    row_id,
                    agent_id,
                    tenant_id,
                    result.session_id,
                )
                return row_id
    except Exception:
        logger.exception("Failed to persist MA run for agent=%s", agent_id)
        return None


def list_ma_runs(
    agent_id: str | None = None,
    tenant_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List recent MA runs, optionally filtered by agent or tenant."""
    try:
        from robothor.db.connection import get_connection

        conditions: list[str] = []
        params: list[Any] = []
        if agent_id:
            conditions.append("agent_id = %s")
            params.append(agent_id)
        if tenant_id:
            conditions.append("tenant_id = %s")
            params.append(tenant_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, agent_id, tenant_id, ma_session_id,
                           output_text, input_tokens, output_tokens,
                           total_cost_usd, outcome_result, duration_ms,
                           created_at
                    FROM ma_runs {where}
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    params,
                )
                cols = [desc[0] for desc in cur.description]
                return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    except Exception:
        logger.exception("Failed to list MA runs")
        return []
