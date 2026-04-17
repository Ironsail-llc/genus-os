"""
Outcome-driven fact invalidation.

When an agent acts on facts and the run fails, those facts are suspect —
either stale, incorrect, or irrelevant. This module:

  1. Logs every fact retrieved during a run (via fact_access_log) so we
     can attribute blame later.
  2. Exposes `bump_failure_for_run(run_id)` to increment outcome_failures
     on all facts touched during that run.
  3. Exposes `compute_outcome_penalty(outcome_failures)` so the decay
     scorer can accelerate retirement of repeatedly-blamed facts.
"""

from __future__ import annotations

import logging

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

# Per-failure penalty applied to decay score, capped so one bad run can't
# destroy a fact outright.
_PER_FAILURE_PENALTY = 0.1
_MAX_PENALTY = 0.4

# After this many failures we also drop confidence so retrieval deprioritizes.
_CONFIDENCE_DROP_THRESHOLD = 3


def log_fact_access(
    run_id: str,
    fact_ids: list[int],
    agent_id: str | None = None,
    tenant_id: str | None = None,
) -> None:
    """Record the fact ids a run consulted. Best-effort, never raises.

    Called from the search_memory tool handler after each successful
    retrieval so we can later attribute failure to specific facts.
    """
    if not run_id or not fact_ids:
        return
    tid = tenant_id or DEFAULT_TENANT
    try:
        from psycopg2.extras import execute_values

        with get_connection() as conn:
            cur = conn.cursor()
            execute_values(
                cur,
                "INSERT INTO fact_access_log (run_id, agent_id, tenant_id, fact_id) VALUES %s",
                [(run_id, agent_id, tid, fid) for fid in fact_ids],
            )
    except Exception as e:
        logger.debug("log_fact_access failed (non-fatal): %s", e)


def compute_outcome_penalty(outcome_failures: int) -> float:
    """Return the decay penalty for N failures, capped at _MAX_PENALTY."""
    if outcome_failures <= 0:
        return 0.0
    return min(_PER_FAILURE_PENALTY * outcome_failures, _MAX_PENALTY)


def bump_failure_for_run(
    run_id: str,
    tenant_id: str | None = None,
) -> dict[str, int]:
    """Increment outcome_failures on every fact touched by a failed run.

    Returns {facts_touched, facts_confidence_dropped} for observability.
    """
    tid = tenant_id or DEFAULT_TENANT
    if not run_id:
        return {"facts_touched": 0, "facts_confidence_dropped": 0}

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE memory_facts
            SET outcome_failures = outcome_failures + 1,
                last_failure_at = NOW(),
                updated_at = NOW()
            WHERE tenant_id = %s
              AND id IN (
                SELECT DISTINCT fact_id FROM fact_access_log
                WHERE run_id = %s AND tenant_id = %s
            )
            """,
            (tid, run_id, tid),
        )
        touched = cur.rowcount

        # Drop confidence on facts that cross the repeated-failure threshold.
        cur.execute(
            """
            UPDATE memory_facts
            SET confidence = GREATEST(0.1, confidence - 0.1),
                updated_at = NOW()
            WHERE tenant_id = %s
              AND outcome_failures >= %s
              AND confidence > 0.1
              AND id IN (
                  SELECT DISTINCT fact_id FROM fact_access_log
                  WHERE run_id = %s AND tenant_id = %s
              )
            """,
            (tid, _CONFIDENCE_DROP_THRESHOLD, run_id, tid),
        )
        dropped = cur.rowcount

    return {"facts_touched": touched, "facts_confidence_dropped": dropped}


def cleanup_old_access_logs(days: int = 30, tenant_id: str | None = None) -> int:
    """Trim the access log — not needed for attribution beyond the decay window.

    ``tenant_id`` bounds the sweep. Nightly maintenance passes it to stay
    inside one tenant's audit data. ``None`` sweeps globally.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        if tenant_id is None:
            cur.execute(
                "DELETE FROM fact_access_log WHERE accessed_at < NOW() - make_interval(days := %s)",
                (days,),
            )
        else:
            cur.execute(
                "DELETE FROM fact_access_log "
                "WHERE tenant_id = %s "
                "AND accessed_at < NOW() - make_interval(days := %s)",
                (tenant_id, days),
            )
        return int(cur.rowcount)
