"""Targeted failure-mode detectors — run periodically from the daemon watchdog.

These are **read-only observers**. They never kill runs. Each detector queries
the DB for a specific signal, compares against a threshold, and fires a
Telegram alert through `robothor.engine.alerts.alert()` when the signal
crosses. In-process dedup prevents alert storms on repeated signals.

Detectors included:
    - repeat_error_detector       — same (agent, error_type) ≥3 in last hour
    - tool_degradation_detector   — tool failure volume or rate spike
    - runaway_burn_detector       — runs with >500K tokens still running
    - zombie_runner_detector      — running rows with no recent step activity

None of these are global timeouts. They alert so the operator (or an agent
with self-diagnosis tools) can decide whether to intervene.

Disable them all via env: ROBOTHOR_DETECTORS_ENABLED=0
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT

logger = logging.getLogger(__name__)


# ── Dedup store ─────────────────────────────────────────────────────────
#
# In-process {fingerprint: epoch_ts}. Keeping it in the daemon's memory is
# fine — a daemon restart clears it and that's OK, because any ongoing
# condition will re-trigger on the next tick.
_DEDUP_TTL_SECONDS = 3600
_dedup: dict[str, float] = {}


def _should_fire(fingerprint: str) -> bool:
    """Return True if this alert fingerprint has not fired in the last hour."""
    now = time.time()
    last = _dedup.get(fingerprint, 0.0)
    if now - last < _DEDUP_TTL_SECONDS:
        return False
    _dedup[fingerprint] = now
    # Opportunistic cleanup — prevent unbounded growth on long uptimes
    if len(_dedup) > 500:
        cutoff = now - _DEDUP_TTL_SECONDS
        for k in list(_dedup):
            if _dedup[k] < cutoff:
                del _dedup[k]
    return True


def detectors_enabled() -> bool:
    return os.environ.get("ROBOTHOR_DETECTORS_ENABLED", "1") != "0"


# ── 1. Repeat-error detector ────────────────────────────────────────────


def check_repeat_errors(
    tenant_id: str = DEFAULT_TENANT,
    hours: int = 1,
    threshold: int = 3,
) -> list[dict[str, Any]]:
    """Return clusters of (agent_id, error_type) with count >= threshold.

    Reuses analytics.get_failure_patterns — we just filter by count.
    """
    from robothor.engine.analytics import get_failure_patterns

    data = get_failure_patterns(hours=hours, tenant_id=tenant_id)
    return [p for p in data.get("patterns", []) if int(p.get("count", 0)) >= threshold]


async def repeat_error_detector(tenant_id: str = DEFAULT_TENANT) -> int:
    """Fire alerts for repeat (agent, error_type) clusters. Returns alerts fired."""
    if not detectors_enabled():
        return 0
    fired = 0
    try:
        clusters = check_repeat_errors(tenant_id=tenant_id)
    except Exception as e:
        logger.debug("repeat_error_detector query failed: %s", e)
        return 0
    from robothor.engine.alerts import alert

    for c in clusters:
        agent = str(c.get("agent_id") or "unknown")
        error_type = str(c.get("error_type") or "unknown")
        count = int(c.get("count") or 0)
        fingerprint = f"repeat:{agent}:{error_type}"
        if not _should_fire(fingerprint):
            continue
        samples = c.get("sample_messages") or []
        sample_text = samples[0][:200] if samples else ""
        body = (
            f"{agent} hit {error_type} {count}× in last hour.\n"
            f"last: {c.get('last_occurrence', '?')}\n"
            f"sample: {sample_text}"
        )
        await alert("warning", f"Repeat errors: {agent}", body)
        fired += 1
    return fired


# ── 2. Tool-dependency degradation ──────────────────────────────────────


def check_tool_degradation(
    hours: int = 1,
    min_failures: int = 5,
    min_calls_for_rate: int = 10,
    failure_rate: float = 0.5,
) -> list[dict[str, Any]]:
    """Return tools with significant failure volume or rate in last hour."""
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT
                tool_name,
                COUNT(*) AS total,
                SUM(CASE WHEN success THEN 0 ELSE 1 END) AS failures
            FROM agent_tool_events
            WHERE created_at > NOW() - make_interval(hours := %s)
              AND tool_name IS NOT NULL
            GROUP BY tool_name
            HAVING COUNT(*) > 0
            """,
            (hours,),
        )
        rows = [dict(r) for r in cur.fetchall()]

    flagged: list[dict[str, Any]] = []
    for r in rows:
        total = int(r["total"] or 0)
        fails = int(r["failures"] or 0)
        if total <= 0:
            continue
        rate = fails / total
        hit_volume = fails >= min_failures
        hit_rate = total >= min_calls_for_rate and rate > failure_rate
        if hit_volume or hit_rate:
            flagged.append(
                {
                    "tool_name": r["tool_name"],
                    "total": total,
                    "failures": fails,
                    "failure_rate": round(rate, 3),
                }
            )
    return flagged


async def tool_degradation_detector() -> int:
    """Alert on degraded tool dependencies."""
    if not detectors_enabled():
        return 0
    fired = 0
    try:
        bad_tools = check_tool_degradation()
    except Exception as e:
        logger.debug("tool_degradation_detector query failed: %s", e)
        return 0
    from robothor.engine.alerts import alert

    for t in bad_tools:
        name = t["tool_name"]
        fingerprint = f"tool_deg:{name}"
        if not _should_fire(fingerprint):
            continue
        body = (
            f"{name}: {t['failures']}/{t['total']} failed in last hour "
            f"(rate {t['failure_rate'] * 100:.0f}%)"
        )
        await alert("warning", f"Tool degradation: {name}", body)
        fired += 1
    return fired


# ── 3. Runaway burn (out-of-band) ───────────────────────────────────────


def check_runaway_burn(
    token_threshold: int = 500_000,
) -> list[dict[str, Any]]:
    """Find running agent_runs that have crossed the token alert threshold.

    Complements the in-loop check in runner._run_loop — this catches runs
    that accumulated tokens in a single very large LLM call between loop
    iterations, or where the in-loop check was bypassed for any reason.
    """
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, agent_id, model_used, input_tokens, output_tokens,
                   started_at,
                   EXTRACT(EPOCH FROM (NOW() - started_at))::int AS elapsed_s
            FROM agent_runs
            WHERE status = 'running'
              AND (COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) >= %s
            ORDER BY (COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) DESC
            LIMIT 10
            """,
            (token_threshold,),
        )
        return [dict(r) for r in cur.fetchall()]


async def runaway_burn_detector() -> int:
    if not detectors_enabled():
        return 0
    fired = 0
    try:
        hot_runs = check_runaway_burn()
    except Exception as e:
        logger.debug("runaway_burn_detector query failed: %s", e)
        return 0
    from robothor.engine.alerts import alert

    for r in hot_runs:
        run_id = str(r["id"])
        fingerprint = f"runaway_oob:{run_id}"
        if not _should_fire(fingerprint):
            continue
        total = (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
        body = (
            f"agent={r.get('agent_id')} model={r.get('model_used')} "
            f"tokens={total:,} elapsed={r.get('elapsed_s')}s run_id={run_id}"
        )
        await alert("warning", "Runaway-burn (out-of-band)", body)
        fired += 1
    return fired


# ── 4. Zombie runner (no step activity) ─────────────────────────────────


def check_zombie_runners(
    stale_minutes: int = 15,
    step_idle_minutes: int = 5,
) -> list[dict[str, Any]]:
    """Running rows older than stale_minutes with no recent step activity.

    Zombie means: agent_runs.status='running' for a while but no
    agent_run_steps rows created in the last step_idle_minutes. Usually a
    runner crash or a hang in setup before any step was recorded.
    Alerts early so the operator can look before the 30-min reaper fires.
    """
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT r.id, r.agent_id, r.started_at,
                   EXTRACT(EPOCH FROM (NOW() - r.started_at))::int AS age_s,
                   (SELECT MAX(created_at) FROM agent_run_steps s
                      WHERE s.run_id = r.id) AS last_step_at
            FROM agent_runs r
            WHERE r.status = 'running'
              AND r.started_at < NOW() - make_interval(mins := %s)
              AND NOT EXISTS (
                  SELECT 1 FROM agent_run_steps s
                  WHERE s.run_id = r.id
                    AND s.created_at > NOW() - make_interval(mins := %s)
              )
            ORDER BY r.started_at ASC
            LIMIT 10
            """,
            (stale_minutes, step_idle_minutes),
        )
        return [dict(r) for r in cur.fetchall()]


async def zombie_runner_detector() -> int:
    if not detectors_enabled():
        return 0
    fired = 0
    try:
        zombies = check_zombie_runners()
    except Exception as e:
        logger.debug("zombie_runner_detector query failed: %s", e)
        return 0
    from robothor.engine.alerts import alert

    for z in zombies:
        run_id = str(z["id"])
        fingerprint = f"zombie:{run_id}"
        if not _should_fire(fingerprint):
            continue
        body = (
            f"agent={z.get('agent_id')} run_id={run_id} "
            f"age={z.get('age_s')}s last_step_at={z.get('last_step_at')}"
        )
        await alert("warning", "Zombie runner (no recent steps)", body)
        fired += 1
    return fired
