"""
Outcome attribution: credit and blame for the facts a run recalled.

  1. Logs every fact retrieved during a run (via fact_access_log) so the
     run's outcome can be attributed to them when it ends.
  2. ``bump_failure_for_run(run_id)`` increments ``outcome_failures`` on every
     fact a failed run recalled.
  3. ``credit_success_for_run(run_id, ...)`` increments ``outcome_successes``
     on every fact a successful run recalled, and ``outcome_citations`` on
     those its final answer demonstrably used (``utility.is_cited``).

The nightly decay pass turns the three counts into ``memory_facts.utility``
(``robothor.memory.utility.compute_utility``), which decay and ranking read.
"""

from __future__ import annotations

import logging
import os

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

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

    # A read tool that writes is still a write. ``search_memory`` is allowed to
    # a benchmark child in BOTH sandbox modes, and it lands here once per fact
    # it consulted — so ~220 graded runs each seeded this table under the
    # production tenant. These rows are the only input to fact_access_rollup
    # and so to the decay scorer, and they are what bump_failure_for_run joins
    # against to decrement confidence on real facts. Skipped, never raised: the
    # contract above says best-effort, and the caller is a retrieval.
    from robothor.engine.run_context import benchmark_write_refused

    if benchmark_write_refused(tid, what="log_fact_access"):
        return

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


def record_fact_access(
    tenant_id: str,
    fact_ids: list[int] | None = None,
    content_hashes: list[str] | None = None,
) -> None:
    """Count one use of each fact recall just returned to an agent. Never raises.

    Raises ``access_count`` and sets ``last_accessed`` — the decay formula's
    use signals, which had no writer until now. Rows are found by id (the
    search tool) or by the active ``(tenant_id, content_hash)`` pair, unique
    per migration 078 (warm-up recall returns text, not ids).

    Synchronous and blocking: callers schedule it off the request path (the
    search handler through the task registry, warm-up through its deferred
    writer). ``search_facts`` never calls it, so a read-only caller such as the
    eval harness searches without side effects.
    """
    ids = [int(i) for i in (fact_ids or []) if i]
    hashes = [h for h in (content_hashes or []) if h]
    if not ids and not hashes:
        return
    tid = tenant_id or DEFAULT_TENANT

    # Same rule as log_fact_access: a benchmark child reads under a real
    # tenant, and its reads must not count as use of real facts.
    from robothor.engine.run_context import benchmark_write_refused

    if benchmark_write_refused(tid, what="record_fact_access"):
        return

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE memory_facts
                SET access_count = COALESCE(access_count, 0) + 1,
                    last_accessed = NOW()
                WHERE tenant_id = %s
                  AND is_active = TRUE
                  AND (id = ANY(%s::int[]) OR content_hash = ANY(%s::text[]))
                """,
                (tid, ids, hashes),
            )
    except Exception as e:
        logger.warning("record_fact_access failed (non-fatal): %s", e)


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

    # The damaging half of the pair: this UPDATEs memory_facts.confidence on
    # rows a benchmark run merely READ. See log_fact_access above.
    from robothor.engine.run_context import benchmark_write_refused

    if benchmark_write_refused(tid, what="bump_failure_for_run"):
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


_NO_CREDIT: dict[str, int] = {"facts_credited": 0, "facts_cited": 0}


def credit_success_for_run(
    run_id: str,
    tenant_id: str | None = None,
    *,
    output_text: str | None = None,
    task_text: str | None = None,
) -> dict[str, int]:
    """Credit every fact a successful run recalled; more for the ones it used.

    Each fact the run's recalls returned (its ``fact_access_log`` rows) gains
    one ``outcome_successes``; those whose distinctive value appears in
    ``output_text`` and not in ``task_text`` also gain one
    ``outcome_citations``. Once per run, however often the run recalled the
    fact. Synchronous: the caller runs it off the event loop at run end.

    Returns {facts_credited, facts_cited} for observability.
    """
    tid = tenant_id or DEFAULT_TENANT
    if not run_id:
        return dict(_NO_CREDIT)

    # Same rule as the failure side: a benchmark run's outcome is not evidence
    # about the facts of a real tenant.
    from robothor.engine.run_context import benchmark_write_refused

    if benchmark_write_refused(tid, what="credit_success_for_run"):
        return dict(_NO_CREDIT)

    from robothor.memory.utility import is_cited

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT f.id, f.fact_text FROM memory_facts f
            WHERE f.tenant_id = %s
              AND f.id IN (
                  SELECT DISTINCT fact_id FROM fact_access_log
                  WHERE run_id = %s AND tenant_id = %s
              )
            """,
            (tid, run_id, tid),
        )
        rows = cur.fetchall()
        if not rows:
            return dict(_NO_CREDIT)
        ids = [int(r[0]) for r in rows]
        cited = [int(r[0]) for r in rows if is_cited(r[1], output_text, task_text)]
        cur.execute(
            """
            UPDATE memory_facts
            SET outcome_successes = COALESCE(outcome_successes, 0) + 1,
                outcome_citations = COALESCE(outcome_citations, 0)
                                    + CASE WHEN id = ANY(%s::int[]) THEN 1 ELSE 0 END,
                last_success_at = NOW()
            WHERE tenant_id = %s AND id = ANY(%s::int[])
            """,
            (cited, tid, ids),
        )
        credited = cur.rowcount

    return {"facts_credited": int(credited or 0), "facts_cited": len(cited)}


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
