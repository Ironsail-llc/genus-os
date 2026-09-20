"""
Run tracking DAL for the Agent Engine.

Records agent runs, steps (audit trail), and schedule state in PostgreSQL.
Follows robothor/crm/dal.py patterns: get_connection(), RealDictCursor, tenant_id.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT, SANDBOX_DENIAL_PREFIX, SANDBOX_DENIED_ERROR_TYPE
from robothor.db.connection import get_connection, read_every_tenant_in_transaction
from robothor.engine.analytics import GENUINE_TIMEOUT_SQL, INTERRUPTED_SQL
from robothor.engine.tools.constants import MAX_TOOL_OUTPUT_CHARS

if TYPE_CHECKING:
    from datetime import datetime

    from robothor.engine.models import AgentRun, RunStep

logger = logging.getLogger(__name__)

# MAX_TOOL_OUTPUT_CHARS is re-exported from robothor.engine.tools.constants,
# where it now lives: the handlers that have to fit their results inside it
# cannot import this module, which reaches for psycopg2 and a live connection.


def _truncate_json(data: Any, max_chars: int = MAX_TOOL_OUTPUT_CHARS) -> Any:
    """Truncate JSON-serializable data to max chars with inline separator.

    Uses 50/50 head/tail split with a clear separator marker so the LLM
    sees natural text rather than JSON keys like preview_head/preview_tail.
    """
    if data is None:
        return None
    text = json.dumps(data, default=str)
    if len(text) <= max_chars:
        return data
    sep = f"\n\n[... truncated {len(text) - max_chars} chars ...]\n\n"
    half = (max_chars - len(sep)) // 2
    truncated = text[:half] + sep + text[-half:]
    return {"_truncated": True, "total_chars": len(text), "content": truncated}


# ─── Runs ─────────────────────────────────────────────────────────────


def create_run(run: AgentRun) -> str:
    """Insert a new agent run. Returns the run ID.

    Retries up to 3 times on transient DB errors (connection drops, pool exhaustion).
    """
    from robothor.engine.retry import retry_sync
    from robothor.engine.runtime.current import run_identity

    def _insert() -> str:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO agent_runs (
                    id, tenant_id, user_id, user_role, agent_id, trigger_type,
                    trigger_detail, correlation_id, status, started_at,
                    model_used, system_prompt_chars, user_prompt_chars,
                    task_text, tools_provided, delivery_mode, parent_run_id,
                    nesting_depth, task_id, person_id, runtime_context
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    run.id,
                    run.tenant_id,
                    # Who caused this run. The columns existed and AgentRun
                    # carried the values, but neither create_run nor update_run
                    # ever wrote them: all 6,144 runs in the 30 days to
                    # 2026-08-27 have an empty user_role. Without this a
                    # federation trigger is indistinguishable from any other
                    # run, which makes the authorization model unauditable in
                    # exactly the case it was built for.
                    run.user_id,
                    run.user_role,
                    run.agent_id,
                    run.trigger_type.value
                    if hasattr(run.trigger_type, "value")
                    else run.trigger_type,
                    run.trigger_detail,
                    run.correlation_id,
                    run.status.value if hasattr(run.status, "value") else run.status,
                    run.started_at,
                    run.model_used,
                    run.system_prompt_chars,
                    run.user_prompt_chars,
                    # The prompt itself, redacted and capped by
                    # `deliverable_contract.task_text_for_column` at the
                    # session door. `user_prompt_chars` above is a count, and
                    # the deliverable contract cannot read a contract from a
                    # count (migration 123).
                    getattr(run, "task_text", None),
                    run.tools_provided,
                    run.delivery_mode,
                    run.parent_run_id,
                    run.nesting_depth,
                    run.task_id,
                    getattr(run, "person_id", None),
                    json.dumps(run_identity(run)),
                ),
            )
        return run.id

    import psycopg2

    return retry_sync(
        _insert,
        max_attempts=3,
        backoff_base=0.5,
        retryable_exceptions=(psycopg2.OperationalError, psycopg2.InterfaceError, ConnectionError),
    )


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
    cache_creation_tokens: int | None = None,
    cache_read_tokens: int | None = None,
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
    outcome_assessment: str | None = None,
    outcome_notes: str | None = None,
    verified_status: str | None = None,
    verification: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> bool:
    """Update an existing run with new fields.

    ``verified_status`` / ``verification`` are the migration-100 claim
    verification columns (see ``robothor.engine.run_verification``); the
    latter is a dict serialised into the ``jsonb`` column.
    """
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
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
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
        "outcome_assessment": outcome_assessment,
        "outcome_notes": outcome_notes,
        "verified_status": verified_status,
        "verification": json.dumps(verification, default=str) if verification else None,
        "task_id": task_id,
    }

    for col, val in field_map.items():
        if val is not None:
            updates.append(f"{col} = %s")
            values.append(val)

    if not updates:
        return True

    values.append(run_id)
    sql = f"UPDATE agent_runs SET {', '.join(updates)} WHERE id = %s"

    from robothor.engine.retry import retry_sync

    def _update() -> bool:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, values)
            return bool(cur.rowcount > 0)

    import psycopg2

    return retry_sync(
        _update,
        max_attempts=3,
        backoff_base=0.5,
        retryable_exceptions=(psycopg2.OperationalError, psycopg2.InterfaceError, ConnectionError),
    )


def get_run(run_id: str) -> dict[str, Any] | None:
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
) -> list[dict[str, Any]]:
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


# ─── Sub-agent tree queries ───────────────────────────────────────────


def get_run_children(run_id: str) -> list[dict[str, Any]]:
    """Get direct child runs of a parent run."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """SELECT id, agent_id, status, trigger_type, nesting_depth,
                      duration_ms, input_tokens, output_tokens, total_cost_usd,
                      started_at, completed_at
               FROM agent_runs
               WHERE parent_run_id = %s
               ORDER BY started_at""",
            (run_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_run_tree(run_id: str) -> dict[str, Any]:
    """Get full execution tree using a recursive CTE.

    Returns the root run with aggregate token/cost totals across all
    descendants, plus a flat list of all runs in the tree.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            WITH RECURSIVE tree AS (
                SELECT id, agent_id, parent_run_id, nesting_depth, status,
                       duration_ms, input_tokens, output_tokens, total_cost_usd,
                       started_at, completed_at
                FROM agent_runs WHERE id = %s
                UNION ALL
                SELECT r.id, r.agent_id, r.parent_run_id, r.nesting_depth, r.status,
                       r.duration_ms, r.input_tokens, r.output_tokens, r.total_cost_usd,
                       r.started_at, r.completed_at
                FROM agent_runs r
                JOIN tree t ON r.parent_run_id = t.id
            )
            SELECT * FROM tree ORDER BY nesting_depth, started_at
            """,
            (run_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return {"root": None, "runs": [], "totals": {}}

    totals = {
        "total_runs": len(rows),
        "total_input_tokens": sum(r.get("input_tokens") or 0 for r in rows),
        "total_output_tokens": sum(r.get("output_tokens") or 0 for r in rows),
        "total_cost_usd": sum(r.get("total_cost_usd") or 0.0 for r in rows),
        "max_nesting_depth": max(r.get("nesting_depth") or 0 for r in rows),
    }

    return {"root": rows[0], "runs": rows, "totals": totals}


# ─── Steps ────────────────────────────────────────────────────────────


#: Whether ``agent_run_steps`` has migration 125's two columns. ``None`` until
#: an insert has told us.
#:
#: A deploy that runs this code against a database without 125 would otherwise
#: lose the WHOLE step trail — LLM steps included, not merely the batched ones.
#: The insert raises ``UndefinedColumn``, which is a ``ProgrammingError`` and so
#: not in ``retry_sync``'s retryable set; ``flush_new_steps_sync`` catches it,
#: falls back to per-step inserts that each raise again, logs one warning per
#: step, and advances ``persisted_step_count`` regardless. Every run keeps
#: running and its ``agent_runs`` row updates normally, so the only symptom is a
#: run viewer with nothing in it.
#:
#: Losing observability to a migration ordering is not a trade worth making, so
#: the writer degrades instead: one loud line naming the migration, then the
#: pre-125 insert. `genus migrate` (or `genus doctor --only db.migrations --fix`)
#: restores the columns and the next process picks them up.
_batch_columns_present: bool | None = None

_BATCH_COLUMNS = ("batch_id", "batch_position")

_MIGRATION_HINT = (
    "agent_run_steps is missing %s — migration 125_agent_run_step_batch has not "
    "been applied to this database. Recording steps WITHOUT the batch columns so "
    "the trail survives; run `genus migrate` (or `genus doctor --only "
    "db.migrations --fix`) and restart to record which tool calls ran together."
)


#: PostgreSQL's SQLSTATE for "column does not exist". Matched on the CODE
#: first, for the reason ``_CHECK_VIOLATION_SQLSTATE`` states below about its
#: own: the code is the standardised part, and it survives a driver swap, a
#: wrapped exception, a pooler and a localised server. The two degrade paths in
#: this module — an action the CHECK does not know (125's neighbour, 124) and a
#: column the table does not have (125) — now read alike, which is the point of
#: them sitting eight hundred lines apart in one file.
_UNDEFINED_COLUMN_SQLSTATE = "42703"


def _missing_batch_columns(exc: BaseException) -> bool:
    """True when this failure is 125 being absent, and not something else.

    Narrow on purpose, in two independent ways. The SQLSTATE says the server
    refused a column that does not exist rather than anything else; the name
    check says it was one of OURS. Any other ``ProgrammingError`` — a different
    missing column, a permission error — must keep raising, because a writer
    that silently degrades on every failure is a writer that cannot tell you it
    is broken.

    The text is still consulted because the column NAME is only in the message,
    and dropping the wrong column would hide a real schema defect. Without the
    SQLSTATE in front of it, though, any error whose text happened to contain
    "column" and "batch_id" — a constraint name, a trigger, a quoted query in
    someone else's error — would have degraded this writer silently.
    """
    if not isinstance(exc, Exception):
        return False
    if getattr(exc, "pgcode", None) != _UNDEFINED_COLUMN_SQLSTATE:
        return False
    text = str(exc).lower()
    return any(name in text for name in _BATCH_COLUMNS)


def _note_missing_batch_columns() -> None:
    """Latch the degradation and say so once, at ERROR."""
    global _batch_columns_present
    if _batch_columns_present is False:
        return
    _batch_columns_present = False
    logger.error(_MIGRATION_HINT, ", ".join(_BATCH_COLUMNS))


def _step_columns() -> tuple[str, str]:
    """``(column list, placeholder list)`` for the step insert."""
    base = (
        "id, run_id, step_number, step_type, "
        "tool_name, tool_input, tool_output, "
        "model, input_tokens, output_tokens, "
        "cache_creation_tokens, cache_read_tokens, "
        "started_at, completed_at, duration_ms, "
        "error_message"
    )
    if _batch_columns_present is False:
        return base, ", ".join(["%s"] * 16)
    return base + ", batch_id, batch_position", ", ".join(["%s"] * 18)


def _step_values(step: RunStep) -> tuple[Any, ...]:
    """The row, with or without the batch columns."""
    row: tuple[Any, ...] = (
        step.id,
        step.run_id,
        step.step_number,
        step.step_type.value if hasattr(step.step_type, "value") else step.step_type,
        step.tool_name,
        json.dumps(step.tool_input, default=str) if step.tool_input else None,
        json.dumps(_truncate_json(step.tool_output), default=str) if step.tool_output else None,
        step.model,
        step.input_tokens,
        step.output_tokens,
        step.cache_creation_tokens,
        step.cache_read_tokens,
        step.started_at,
        step.completed_at,
        step.duration_ms,
        step.error_message,
    )
    if _batch_columns_present is False:
        return row
    return (*row, step.batch_id, step.batch_position)


def create_step(step: RunStep) -> str:
    """Insert a new run step (append-only audit trail). Returns step ID.

    Retries up to 3 times on transient DB errors (matching create_run pattern).
    """
    from robothor.engine.retry import retry_sync

    def _insert() -> str:
        columns, placeholders = _step_columns()
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                f"INSERT INTO agent_run_steps ({columns}) VALUES ({placeholders})",  # noqa: S608
                _step_values(step),
            )
        return step.id

    def _insert_or_degrade() -> str:
        try:
            return _insert()
        except Exception as exc:
            if not _missing_batch_columns(exc):
                raise
            _note_missing_batch_columns()
            return _insert()

    import psycopg2

    return retry_sync(
        _insert_or_degrade,
        max_attempts=3,
        backoff_base=0.5,
        retryable_exceptions=(psycopg2.OperationalError, psycopg2.InterfaceError, ConnectionError),
    )


def create_steps_batch(steps: list[RunStep]) -> int:
    """Insert multiple run steps in a single DB round-trip. Returns count inserted.

    Uses psycopg2 execute_values for efficiency — one INSERT instead of N.
    Retries up to 3 times on transient DB errors (matching create_step pattern).
    """
    if not steps:
        return 0

    from robothor.engine.retry import retry_sync

    def _insert_batch() -> int:
        columns, _ = _step_columns()
        rows = [_step_values(step) for step in steps]
        with get_connection() as conn:
            cur = conn.cursor()
            from psycopg2.extras import execute_values

            execute_values(
                cur,
                f"INSERT INTO agent_run_steps ({columns}) "  # noqa: S608
                "VALUES %s ON CONFLICT (id) DO NOTHING",
                rows,
            )
            return len(rows)

    def _insert_batch_or_degrade() -> int:
        try:
            return _insert_batch()
        except Exception as exc:
            if not _missing_batch_columns(exc):
                raise
            _note_missing_batch_columns()
            return _insert_batch()

    import psycopg2

    return retry_sync(
        _insert_batch_or_degrade,
        max_attempts=3,
        backoff_base=0.5,
        retryable_exceptions=(psycopg2.OperationalError, psycopg2.InterfaceError, ConnectionError),
    )


def list_steps(run_id: str) -> list[dict[str, Any]]:
    """List all steps for a run, ordered by step number."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM agent_run_steps WHERE run_id = %s ORDER BY step_number",
            (run_id,),
        )
        return [dict(r) for r in cur.fetchall()]


# ─── Schedules ────────────────────────────────────────────────────────


def get_schedule(agent_id: str) -> dict[str, Any] | None:
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
    timezone: str = "America/New_York",
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
        return bool(cur.rowcount > 0)


def list_schedules(
    enabled_only: bool = False,
    tenant_id: str = DEFAULT_TENANT,
) -> list[dict[str, Any]]:
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


def delete_stale_schedules(
    active_agent_ids: set[str],
    tenant_id: str = DEFAULT_TENANT,
) -> list[str]:
    """Delete schedule rows for agents that no longer have manifests.

    Returns list of deleted agent_ids.
    """
    if not active_agent_ids:
        return []

    with get_connection() as conn:
        cur = conn.cursor()
        placeholders = ",".join(["%s"] * len(active_agent_ids))
        cur.execute(
            f"DELETE FROM agent_schedules WHERE tenant_id = %s "
            f"AND agent_id NOT IN ({placeholders}) "
            f"RETURNING agent_id",
            [tenant_id, *active_agent_ids],
        )
        deleted = [row[0] for row in cur.fetchall()]
        return deleted


#: Ceiling on a stored tool-failure reason. Long enough to carry a real
#: message and a short traceback tail, short enough that one pathological
#: payload cannot bloat a table that takes an insert per tool call.
MAX_TOOL_ERROR_CHARS = 500


def log_tool_event(
    run_id: str,
    tool_name: str,
    duration_ms: int,
    success: bool,
    step_id: str | None = None,
    error_type: Any = None,
    error_message: str | None = None,
) -> None:
    """Log a tool invocation event for observability.

    ``error_message`` records WHY the call failed. Without it the table says
    only THAT it failed: on 2026-08-27, 68.4% of failures over 7 days were
    ``error_type='unknown'`` with no recoverable cause anywhere, so the
    degradation detector paged about tools whose failure nobody could
    diagnose. Stored only on failure, and truncated.

    Two more corrections made here, at the last point before the row is
    written, rather than upstream in every caller:

    * A message that starts with ``SANDBOX_DENIAL_PREFIX`` (the benchmark
      sandbox's write-refusal, e.g. "benchmark sandbox: create_task writes
      are disabled") is always classified ``error_type='sandbox_denied'``,
      overriding whatever the caller passed. Measured 2026-09-11: this is
      the benchmark harness being correctly refused, not a broken tool —
      ``check_tool_degradation`` excludes the label so it stops paging
      about it.
    * A failed call with no ``error_message`` (e.g. a guardrail block that
      never forwarded its refusal reason) falls back to ``error_type``,
      then to a fixed placeholder — a row that says only THAT a call
      failed, with a database column that could have said why left NULL,
      is the same defect this function exists to fix.
    """
    # Accept an ErrorType enum, a bare string, or None. Callers should not
    # have to know this enum's shape to record an event about it.
    kind: str | None = getattr(error_type, "value", None)
    if kind is None and error_type is not None:
        kind = str(error_type)

    reason: str | None = None
    if not success:
        text = str(error_message).strip() if error_message else ""
        if text.startswith(SANDBOX_DENIAL_PREFIX):
            kind = SANDBOX_DENIED_ERROR_TYPE
        reason = (text or kind or "failed without a message")[:MAX_TOOL_ERROR_CHARS]
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO agent_tool_events
                    (run_id, step_id, tool_name, duration_ms, success, error_type, error_message)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (run_id, step_id, tool_name, duration_ms, success, kind, reason),
            )
    except Exception as e:
        logger.debug("Failed to log tool event: %s", e)


#: Actions every instance has allowed since migration 001 (plus ``observed``
#: from 079). An action OUTSIDE this set can be refused by the CHECK on a box
#: that has not run the migration that added it — which is every box between a
#: deploy and ``genus migrate``.
_LEGACY_GUARDRAIL_ACTIONS = frozenset({"blocked", "warned", "allowed", "observed"})

#: What a refused action degrades TO. Allowed since the table existed, so the
#: row lands on any instance in any state.
_FALLBACK_GUARDRAIL_ACTION = "warned"

#: The migration that adds the newest action value, named in the one warning
#: this module prints.
_ACTION_MIGRATION = "124_guardrail_context_overflow"

#: One line per process, not one per event: this is on the LLM path and fires
#: as often as the control does.
_warned_about_action_check = False


def reset_guardrail_action_warning() -> None:
    """Re-arm the once-per-process warning. For tests."""
    global _warned_about_action_check
    _warned_about_action_check = False


#: PostgreSQL's SQLSTATE for a CHECK violation. Matched on the CODE rather
#: than on ``psycopg2.errors.CheckViolation``, because the code is the part of
#: this that is standardised: it survives a driver swap, a wrapped exception
#: and a pooler, and it needs no second import of a package whose error classes
#: ship no type stubs.
_CHECK_VIOLATION_SQLSTATE = "23514"


def _insert_guardrail_event(row: tuple[Any, ...]) -> None:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO agent_guardrail_events (
                run_id, step_number, guardrail_name, action, tool_name, reason, mode
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            row,
        )


def log_guardrail_event(
    run_id: str,
    guardrail_name: str,
    action: str,
    *,
    tool_name: str | None = None,
    reason: str | None = None,
    mode: str | None = None,
    step_number: int = 0,
) -> None:
    """Record a guardrail decision in ``agent_guardrail_events`` (best-effort).

    ``action`` is ``blocked`` | ``warned`` | ``allowed`` | ``observed`` |
    ``context_overflow``. ``observed`` records a SHADOW decision produced in
    observe mode — the guardrail *would* have acted but the enforcement flag
    let it through — so the operator can inspect impact (via the health
    dashboard) before promoting a flag to ``enforce``. ``context_overflow``
    (migration 124) records the engine SHRINKING a conversation that did not
    fit the model it was about to be sent to. ``mode`` records the enforcement
    mode (off/observe/alert/enforce) that produced the event.

    The table existed since migration 014 and is read by ``health.py`` but was
    never written until this writer. Logging is best-effort: a DB failure here
    must never break the agent run.

    One failure is NOT swallowed quietly: an action the instance's CHECK does
    not know yet. Between a deploy and ``genus migrate`` the row would simply
    vanish, and an evidence table that reports zero firings is how this project
    has repeatedly shipped a control that does nothing. The row is re-written
    as ``warned`` — the guardrail NAME is what the evidence queries key on, and
    it is unchanged — and the operator is told once which migration to run.
    """
    row = (run_id, step_number, guardrail_name, action, tool_name, reason, mode)
    try:
        _insert_guardrail_event(row)
        return
    except Exception as e:
        refused = getattr(e, "pgcode", None) == _CHECK_VIOLATION_SQLSTATE
        if not refused or action in _LEGACY_GUARDRAIL_ACTIONS:
            logger.debug("Failed to log guardrail event: %s", e)
            return
    _degrade_guardrail_action(row, action)


def _degrade_guardrail_action(row: tuple[Any, ...], action: str) -> None:
    """Re-write the refused row with an action every instance accepts."""
    global _warned_about_action_check
    if not _warned_about_action_check:
        _warned_about_action_check = True
        logger.warning(
            "agent_guardrail_events does not accept action %r on this instance — "
            "recording it as %r instead. Run `genus migrate` to apply %s, or the "
            "evidence table will under-report this control.",
            action,
            _FALLBACK_GUARDRAIL_ACTION,
            _ACTION_MIGRATION,
        )
    try:
        _insert_guardrail_event((*row[:3], _FALLBACK_GUARDRAIL_ACTION, *row[4:]))
    except Exception as e:  # noqa: BLE001 — telemetry never breaks a run
        logger.debug("Failed to log degraded guardrail event: %s", e)


def get_tool_stats(hours: int = 24) -> list[dict[str, Any]]:
    """Get aggregated tool stats over the last N hours."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT
                tool_name,
                COUNT(*) as total_calls,
                COUNT(*) FILTER (WHERE success) as successes,
                COUNT(*) FILTER (WHERE NOT success) as failures,
                AVG(duration_ms) as avg_duration_ms,
                MAX(duration_ms) as max_duration_ms,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms) as p95_duration_ms
            FROM agent_tool_events
            WHERE created_at > NOW() - INTERVAL '%s hours'
              AND (error_type IS NULL OR error_type <> %s)
            GROUP BY tool_name
            ORDER BY total_calls DESC
            """,
            (hours, SANDBOX_DENIED_ERROR_TYPE),
        )
        return [dict(r) for r in cur.fetchall()]


def get_agent_stats(
    agent_id: str,
    hours: int = 24,
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any]:
    """Get aggregated stats for an agent over the last N hours.

    Sub-agent runs stay counted here — this is the ``/costs`` surface and
    their spend is real money. Benchmark-harness spend is broken out into
    ``benchmark_runs`` / ``benchmark_cost_usd`` instead of being billed to the
    agent, so a fleet benchmark sweep can no longer read as an agent going
    expensive overnight ($29.93 of $78.03 over 30 days on this instance).
    """
    from robothor.engine.analytics import benchmark_run_filter, exclude_benchmark_filter

    no_bench = exclude_benchmark_filter()
    bench_only = benchmark_run_filter()

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"""
            SELECT
                COUNT(*) as total_runs,
                COUNT(*) FILTER (WHERE status = 'completed') as completed,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                COUNT(*) FILTER (WHERE {GENUINE_TIMEOUT_SQL}) as timeouts,
                COUNT(*) FILTER (WHERE {INTERRUPTED_SQL}) as interrupted,
                AVG(duration_ms) FILTER (WHERE status = 'completed') as avg_duration_ms,
                SUM(input_tokens) as total_input_tokens,
                SUM(output_tokens) as total_output_tokens,
                SUM(total_cost_usd) as total_cost_usd
            FROM agent_runs
            WHERE agent_id = %s
              AND tenant_id = %s
              AND {no_bench}
              AND created_at > NOW() - INTERVAL '%s hours'
            """,  # noqa: S608 — no_bench is a literal, not user input
            (agent_id, tenant_id, hours),
        )
        row = cur.fetchone()
        stats = dict(row) if row else {}

        # None until read: "unknown" and "no benchmark spend" are different
        # answers, and only one of them is worth acting on. See
        # analytics._benchmark_spend.
        stats["benchmark_runs"] = None
        stats["benchmark_cost_usd"] = None
        try:
            read_every_tenant_in_transaction(conn)
            cur.execute(
                f"""
                SELECT
                    COUNT(*) as benchmark_runs,
                    COALESCE(SUM(total_cost_usd), 0) as benchmark_cost_usd
                FROM agent_runs
                WHERE agent_id = %s
                -- Deliberately NOT tenant-scoped: `bench_only` already
                -- isolates benchmark rows by trigger_detail, and since
                -- 2026-09-13 every graded task run executes as the
                -- `benchmark-sandbox` tenant, so binding the owning tenant
                -- here reported every agent's benchmark spend as zero.
                  AND {bench_only}
                  AND created_at > NOW() - INTERVAL '%s hours'
                """,  # noqa: S608
                (agent_id, hours),
            )
            brow = cur.fetchone() or {}
            stats["benchmark_runs"] = int(brow.get("benchmark_runs") or 0)
            stats["benchmark_cost_usd"] = float(brow.get("benchmark_cost_usd") or 0.0)
        except Exception as e:
            logger.error(
                "benchmark break-out unreadable for %s (%s): /costs reports UNKNOWN "
                "benchmark spend rather than zero",
                agent_id,
                e,
            )

        return stats
