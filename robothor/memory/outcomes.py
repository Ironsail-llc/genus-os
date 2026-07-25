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
import os

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

# Per-failure penalty applied to decay score, capped so one bad run can't
# destroy a fact outright.
_PER_FAILURE_PENALTY = 0.1
_MAX_PENALTY = 0.4

# How long raw per-run access rows are kept before being folded into
# fact_access_rollup. Historically hardcoded to 30 days at the call site, which
# meant the decay formula's only input was being destroyed faster than anything
# consumed it.
_DEFAULT_RETENTION_DAYS = 30

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


def access_log_retention_days() -> int:
    """Days of raw access log to keep, from MEMORY_ACCESS_LOG_RETENTION_DAYS.

    Falls back to the historical 30 on anything unparseable, and refuses
    non-positive values: a 0-day window would delete the entire log on the next
    nightly pass, so a bad config must fail safe rather than fail destructive.
    """
    raw = os.environ.get("MEMORY_ACCESS_LOG_RETENTION_DAYS", "").strip()
    if not raw:
        return _DEFAULT_RETENTION_DAYS
    try:
        days = int(raw)
    except ValueError:
        logger.warning(
            "MEMORY_ACCESS_LOG_RETENTION_DAYS=%r is not an integer; using %d",
            raw,
            _DEFAULT_RETENTION_DAYS,
        )
        return _DEFAULT_RETENTION_DAYS
    if days <= 0:
        logger.warning(
            "MEMORY_ACCESS_LOG_RETENTION_DAYS=%d is not positive; using %d",
            days,
            _DEFAULT_RETENTION_DAYS,
        )
        return _DEFAULT_RETENTION_DAYS
    return days


# Roll the doomed rows into the lifetime aggregate and delete them in one
# statement. Postgres runs a data-modifying CTE exactly once and to completion,
# so `doomed` is materialised before `agg` and the final count read it — there
# is no window where rows are deleted but unaccounted for, and no way for a
# crash between two statements to lose the counts.
#
# ON CONFLICT accumulates. Overwriting here would look correct on the first
# night and silently discard every prior night's history on the second.
_ROLLUP_AND_DELETE = """
WITH doomed AS (
    DELETE FROM fact_access_log
    WHERE accessed_at < NOW() - make_interval(days => %(days)s)
      AND (%(tenant)s::text IS NULL OR tenant_id = %(tenant)s)
    RETURNING fact_id, tenant_id, accessed_at
), agg AS (
    SELECT fact_id,
           tenant_id,
           count(*)          AS n,
           min(accessed_at)  AS first_at,
           max(accessed_at)  AS last_at
    FROM doomed
    GROUP BY fact_id, tenant_id
), rolled AS (
    INSERT INTO fact_access_rollup AS r
        (fact_id, tenant_id, access_count, first_accessed_at, last_accessed_at, updated_at)
    SELECT fact_id, tenant_id, n, first_at, last_at, NOW() FROM agg
    ON CONFLICT (fact_id, tenant_id) DO UPDATE
    SET access_count      = r.access_count + EXCLUDED.access_count,
        first_accessed_at = LEAST(r.first_accessed_at, EXCLUDED.first_accessed_at),
        last_accessed_at  = GREATEST(r.last_accessed_at, EXCLUDED.last_accessed_at),
        updated_at        = NOW()
    RETURNING 1
)
SELECT (SELECT count(*) FROM doomed) AS deleted,
       (SELECT count(*) FROM rolled) AS facts_rolled
"""


def cleanup_old_access_logs(days: int | None = None, tenant_id: str | None = None) -> int:
    """Trim the raw access log, preserving lifetime counts in the roll-up.

    ``days`` defaults to :func:`access_log_retention_days`. ``tenant_id`` bounds
    the sweep; ``None`` sweeps globally.

    Returns the number of raw rows removed. The counts themselves are not lost —
    they are folded into ``fact_access_rollup`` in the same statement.
    """
    window = access_log_retention_days() if days is None else days
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(_ROLLUP_AND_DELETE, {"days": window, "tenant": tenant_id})
        row = cur.fetchone()
        if row is None:
            return 0
        deleted, facts_rolled = int(row[0]), int(row[1])
        if deleted:
            logger.info(
                "access log GC: %d rows removed, %d facts rolled up (window=%dd, tenant=%s)",
                deleted,
                facts_rolled,
                window,
                tenant_id or "*",
            )
        return deleted
