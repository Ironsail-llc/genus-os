"""Targeted failure-mode detectors — run periodically from the daemon watchdog.

These are **read-only observers**. They never kill runs. Each detector queries
the DB for a specific signal, compares against a threshold, and fires an
alert through `robothor.engine.alerts.alert()` when the signal crosses
(warning-level alerts land in the crm_agent_notifications digest, not a
Telegram page). In-process dedup prevents alert storms on repeated signals.

Detectors included:
    - repeat_error_detector             — same (agent, error_type) ≥3 in last hour
    - tool_degradation_detector         — tool failure volume or rate spike
    - tool_outage_detector              — tool ~totally dead over a multi-day window
    - primary_model_unreached_detector  — runs served by a non-primary model
    - runaway_burn_detector             — runs with >500K tokens still running
    - zombie_runner_detector            — running rows with no recent step activity
    - stuck_workflow_detector           — workflow_runs 'running' beyond timeout+grace
    - workflow_failure_streak_detector  — same workflow failing ≥3 consecutive runs

None of these are global timeouts. They alert so the operator (or an agent
with self-diagnosis tools) can decide whether to intervene.

Disable them all via env: ROBOTHOR_DETECTORS_ENABLED=0
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT

logger = logging.getLogger(__name__)

# Tools served by the vision service (tools/handlers/vision.py proxies these
# over HTTP). When the operator has administratively disabled that service,
# their failures are expected — paging about them is noise nobody can act on.
_VISION_TOOLS = frozenset(
    {
        "look",
        "who_is_here",
        "enroll_face",
        "enroll_face_from_image",
        "list_enrolled_faces",
        "unenroll_face",
        "set_vision_mode",
    }
)


def _vision_service_disabled() -> bool:
    """True when the vision service is administratively disabled.

    Reads the mode file the vision service persists its mode to
    (``<state_dir>/vision_mode.txt`` — see robothor/vision/service.py
    ``_mode_file``). The ~5 lines of path convention are deliberately
    duplicated here instead of imported: the engine must not import the
    vision package (heavy cv2/numpy deps, separate service boundary).
    """
    try:
        state_dir = Path(
            os.environ.get("STATE_DIR")
            or os.environ.get("ROBOTHOR_MEMORY_DIR")
            or (Path.home() / "robothor" / "memory")
        )
        mode_file = state_dir / "vision_mode.txt"
        return mode_file.is_file() and mode_file.read_text().strip() == "disabled"
    except Exception:
        return False


# Env var declaring tool outages the operator has already decided about:
# "tool_name:reason,tool_name:reason". See declared_tool_outages().
_DECLARED_OUTAGES_ENV = "ROBOTHOR_DECLARED_TOOL_OUTAGES"


def declared_tool_outages() -> dict[str, str]:
    """Return ``{tool_name: reason}`` for outages that are already known.

    A sustained-outage alert is only worth sending for an outage nobody has
    decided about yet. Suppression therefore has exactly two sources, both
    explicit, and both carrying a reason that gets logged when they fire:

    * ``ROBOTHOR_DECLARED_TOOL_OUTAGES`` — ``"tool:reason,tool:reason"``, set on
      the engine unit when a dependency is knowingly dead (contract ended,
      credential revoked pending rotation). Declaring it silences the alert and
      records why.
    * The vision tool set, but only while the vision service reports itself
      administratively disabled (``_vision_service_disabled``). That suppression
      expires on its own when the service is re-armed — it is not a permanent
      exemption, and no other tool gets one.

    Anything not returned here alerts. There is no third, silent path.

    Returns:
        Mapping of tool name to the declared reason for its outage.
    """
    declared: dict[str, str] = {}
    raw = os.environ.get(_DECLARED_OUTAGES_ENV, "")
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, _, reason = entry.partition(":")
        name = name.strip()
        if not name:
            continue
        declared[name] = reason.strip() or f"declared in {_DECLARED_OUTAGES_ENV} (no reason given)"
    if _vision_service_disabled():
        for tool in _VISION_TOOLS:
            declared.setdefault(tool, "vision service is administratively disabled")
    return declared


# ── Dedup store ─────────────────────────────────────────────────────────
#
# In-process {fingerprint: epoch_ts}. Keeping it in the daemon's memory is
# fine — a daemon restart clears it and that's OK, because any ongoing
# condition will re-trigger on the next tick.
_DEDUP_TTL_SECONDS = 3600
# Slow-moving signals (a tool dead for two weeks, a primary model unreachable
# for days) re-evaluate to the same conclusion on every tick. Repeating them
# hourly would be a pager storm for a condition that changes on a scale of
# days, so they dedup for a day instead of an hour.
_SLOW_DEDUP_TTL_SECONDS = 86400
_dedup: dict[str, float] = {}


def _should_fire(fingerprint: str, ttl_seconds: int = _DEDUP_TTL_SECONDS) -> bool:
    """Return True if this alert fingerprint has not fired within ``ttl_seconds``."""
    now = time.time()
    last = _dedup.get(fingerprint, 0.0)
    if now - last < ttl_seconds:
        return False
    _dedup[fingerprint] = now
    # Opportunistic cleanup — prevent unbounded growth on long uptimes. The
    # cutoff uses the longest TTL in play so a day-scale fingerprint is never
    # evicted early (which would let its alert re-fire).
    if len(_dedup) > 500:
        cutoff = now - max(ttl_seconds, _SLOW_DEDUP_TTL_SECONDS)
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
        if not await alert("warning", f"Repeat errors: {agent}", body):
            logger.warning("Alert delivery failed for %s", fingerprint)
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

    vision_disabled: bool | None = None  # lazy — one stat() per tick at most
    for t in bad_tools:
        name = t["tool_name"]
        if name in _VISION_TOOLS:
            if vision_disabled is None:
                vision_disabled = _vision_service_disabled()
            if vision_disabled:
                logger.info(
                    "Suppressing tool-degradation alert for %s — "
                    "vision service is administratively disabled",
                    name,
                )
                continue
        fingerprint = f"tool_deg:{name}"
        if not _should_fire(fingerprint):
            continue
        body = (
            f"{name}: {t['failures']}/{t['total']} failed in last hour "
            f"(rate {t['failure_rate'] * 100:.0f}%)"
        )
        if not await alert("warning", f"Tool degradation: {name}", body):
            logger.warning("Alert delivery failed for %s", fingerprint)
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
        if not await alert("warning", "Runaway-burn (out-of-band)", body):
            logger.warning("Alert delivery failed for %s", fingerprint)
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
        if not await alert("warning", "Zombie runner (no recent steps)", body):
            logger.warning("Alert delivery failed for %s", fingerprint)
        fired += 1
    return fired


# ── 5. Stuck workflow runs ──────────────────────────────────────────────


def check_stuck_workflow_runs(
    timeout_seconds: int = 900,
    grace_seconds: int = 600,
) -> list[dict[str, Any]]:
    """workflow_runs still 'running' beyond the workflow timeout + grace.

    Per-workflow timeouts live in YAML (not the DB), so the threshold uses
    the platform's maximum workflow timeout (900s default) plus a grace
    period. Anything 'running' past that is an orphan — usually an engine
    restart mid-run whose CancelledError finalizer could not persist.
    Alert-only: the daemon reaper owns the state transition.
    """
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, workflow_id, started_at,
                   EXTRACT(EPOCH FROM (NOW() - started_at))::int AS age_s
            FROM workflow_runs
            WHERE status = 'running'
              AND started_at < NOW() - make_interval(secs => %s)
            ORDER BY started_at ASC
            LIMIT 10
            """,
            (timeout_seconds + grace_seconds,),
        )
        return [dict(r) for r in cur.fetchall()]


async def stuck_workflow_detector() -> int:
    """Alert on workflow_runs stuck 'running' beyond timeout + grace."""
    if not detectors_enabled():
        return 0
    fired = 0
    try:
        stuck = check_stuck_workflow_runs()
    except Exception as e:
        logger.debug("stuck_workflow_detector query failed: %s", e)
        return 0
    from robothor.engine.alerts import alert

    for s in stuck:
        run_id = str(s["id"])
        fingerprint = f"workflow-stuck:{run_id}"
        if not _should_fire(fingerprint):
            continue
        body = (
            f"workflow={s.get('workflow_id')} run_id={run_id} "
            f"age={s.get('age_s')}s started_at={s.get('started_at')}"
        )
        await alert("warning", "Stuck workflow run", body)
        fired += 1
    return fired


# ── 6. Workflow failure streaks ─────────────────────────────────────────


def check_workflow_failure_streaks(
    threshold: int = 3,
) -> list[dict[str, Any]]:
    """Workflows whose last ``threshold`` terminal runs all failed/timed out.

    'cancelled' and 'skipped' rows are excluded from the window — a shutdown
    mid-run says nothing about the workflow's health either way.
    """
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT workflow_id,
                   COUNT(*) AS streak,
                   (ARRAY_AGG(error_message ORDER BY rn))[1] AS last_error
            FROM (
                SELECT workflow_id, status, error_message,
                       ROW_NUMBER() OVER (
                           PARTITION BY workflow_id ORDER BY started_at DESC
                       ) AS rn
                FROM workflow_runs
                WHERE status IN ('completed', 'failed', 'timeout')
            ) recent
            WHERE rn <= %s
            GROUP BY workflow_id
            HAVING COUNT(*) >= %s
               AND BOOL_AND(status IN ('failed', 'timeout'))
            LIMIT 10
            """,
            (threshold, threshold),
        )
        return [dict(r) for r in cur.fetchall()]


async def workflow_failure_streak_detector() -> int:
    """Alert when the same workflow has failed several consecutive runs."""
    if not detectors_enabled():
        return 0
    fired = 0
    try:
        streaks = check_workflow_failure_streaks()
    except Exception as e:
        logger.debug("workflow_failure_streak_detector query failed: %s", e)
        return 0
    from robothor.engine.alerts import alert

    for s in streaks:
        workflow_id = str(s.get("workflow_id") or "unknown")
        fingerprint = f"workflow-failing:{workflow_id}"
        if not _should_fire(fingerprint):
            continue
        last_error = str(s.get("last_error") or "")[:300]
        body = f"last {s.get('streak')} runs all failed/timed out.\nlast error: {last_error}"
        await alert("warning", f"Workflow failing repeatedly: {workflow_id}", body)
        fired += 1
    return fired


# ── 7. Sustained tool outage ────────────────────────────────────────────
#
# The complement of detector 2. `tool_degradation_detector` looks at one hour
# and needs ~5 failures in it, so it only ever sees an *acute* problem: a busy
# tool that started erroring. A tool called twice a day can be 100% dead
# forever without ever putting 5 failures in the same hour — which is exactly
# how apollo_search_people failed 32/32 (error_type=auth) across 14 days with
# nothing alerting. This detector trades resolution for reach: a long window,
# a volume floor so it cannot fire on noise, and a failure ratio so high that
# firing means "this dependency is gone", not "this dependency is flaky".

_OUTAGE_WINDOW_HOURS = 168  # 7 days — long enough for a twice-a-day tool
_OUTAGE_MIN_CALLS = 8  # below this the sample says nothing
_OUTAGE_FAILURE_RATIO = 0.95  # ~total failure, not degradation
_OUTAGE_LOOKBACK_DAYS = 14  # horizon for "when did it last work?"
_OUTAGE_CRITICAL_DAYS = 3.0  # dead this long stops being a warning


def check_tool_outage(
    window_hours: int = _OUTAGE_WINDOW_HOURS,
    min_calls: int = _OUTAGE_MIN_CALLS,
    failure_ratio: float = _OUTAGE_FAILURE_RATIO,
    lookback_days: int = _OUTAGE_LOOKBACK_DAYS,
    critical_days: float = _OUTAGE_CRITICAL_DAYS,
) -> list[dict[str, Any]]:
    """Return tools that are ~totally failing over a multi-day window.

    One grouped scan of ``agent_tool_events`` over ``lookback_days`` (indexed on
    ``created_at``, and the group cardinality is the tool count). The window
    counters decide whether it is an outage; the lookback columns decide how
    old it is. Thresholds are applied in Python so they stay testable without a
    database — same split as :func:`check_tool_degradation`.

    Args:
        window_hours: Window whose calls decide outage vs. healthy.
        min_calls: Minimum calls in the window before any verdict is possible.
        failure_ratio: Failure share at or above which the tool counts as out.
        lookback_days: How far back to look for the last successful call.
        critical_days: Outage age at or beyond which severity is 'critical'.

    Returns:
        One dict per outage: tool_name, total, failures, failure_rate,
        error_type (dominant), outage_days, last_success_at, severity.
    """
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT
                tool_name,
                COUNT(*) FILTER (
                    WHERE created_at > NOW() - make_interval(hours => %(hours)s)
                ) AS total,
                COUNT(*) FILTER (
                    WHERE created_at > NOW() - make_interval(hours => %(hours)s)
                      AND NOT success
                ) AS failures,
                MODE() WITHIN GROUP (ORDER BY COALESCE(error_type, 'unknown'))
                    FILTER (
                        WHERE created_at > NOW() - make_interval(hours => %(hours)s)
                          AND NOT success
                    ) AS error_type,
                MAX(created_at) FILTER (WHERE success) AS last_success_at,
                EXTRACT(EPOCH FROM (NOW() - COALESCE(
                    MAX(created_at) FILTER (WHERE success),
                    MIN(created_at) FILTER (WHERE NOT success)
                ))) / 86400.0 AS outage_days
            FROM agent_tool_events
            WHERE created_at > NOW() - make_interval(days => %(lookback_days)s)
              AND tool_name IS NOT NULL
            GROUP BY tool_name
            HAVING COUNT(*) FILTER (
                WHERE created_at > NOW() - make_interval(hours => %(hours)s)
            ) > 0
            """,
            {"hours": window_hours, "lookback_days": lookback_days},
        )
        rows = [dict(r) for r in cur.fetchall()]

    out: list[dict[str, Any]] = []
    for r in rows:
        total = int(r["total"] or 0)
        failures = int(r["failures"] or 0)
        if total <= 0 or total < min_calls:
            continue
        rate = failures / total
        if rate < failure_ratio:
            continue
        # Clamped at the lookback horizon: a tool dead longer than that reads
        # as exactly lookback_days old, which is already well past critical.
        outage_days = float(r["outage_days"] or 0.0)
        out.append(
            {
                "tool_name": r["tool_name"],
                "total": total,
                "failures": failures,
                "failure_rate": round(rate, 3),
                "error_type": r["error_type"] or "unknown",
                "last_success_at": r["last_success_at"],
                "outage_days": round(outage_days, 2),
                "severity": "critical" if outage_days >= critical_days else "warning",
            }
        )
    out.sort(key=lambda t: (-t["outage_days"], t["tool_name"]))
    return out


async def tool_outage_detector() -> int:
    """Alert on tools that have been ~totally failing for days. Returns alerts fired."""
    if not detectors_enabled():
        return 0
    fired = 0
    try:
        outages = check_tool_outage()
    except Exception as e:
        logger.debug("tool_outage_detector query failed: %s", e)
        return 0
    from robothor.engine.alerts import alert

    declared = declared_tool_outages()
    for t in outages:
        name = str(t["tool_name"])
        if name in declared:
            logger.info(
                "Suppressing tool-outage alert for %s — declared outage: %s",
                name,
                declared[name],
            )
            continue
        severity = str(t["severity"])
        # Severity is part of the fingerprint so a warning already sent cannot
        # swallow the escalation to critical three days later.
        if not _should_fire(f"tool_outage:{name}:{severity}", _SLOW_DEDUP_TTL_SECONDS):
            continue
        window_days = _OUTAGE_WINDOW_HOURS // 24
        last_ok = t.get("last_success_at") or "never in the lookback window"
        body = (
            f"{name}: {t['failures']}/{t['total']} calls failed "
            f"({t['failure_rate'] * 100:.0f}%) over the last {window_days}d.\n"
            f"dominant error_type: {t['error_type']}\n"
            f"out for {t['outage_days']:.1f} days (last success: {last_ok})"
        )
        title = f"Tool outage: {name}"
        if severity == "critical":
            title = f"Tool dead {t['outage_days']:.0f}d: {name}"
        if not await alert(severity, title, body):
            logger.warning("Alert delivery failed for tool_outage:%s", name)
        fired += 1
    return fired


# ── 8. Primary model unreached ──────────────────────────────────────────
#
# `llm_client` logs "PRIMARY model failed, falling back" at warning level and
# the run then completes normally, so a fleet can spend days on its fallback
# chain with every run green. `agent_runs` records what actually served the
# run; nothing read it until now.

_MODEL_WINDOW_HOURS = 168  # 7 days
_MODEL_MIN_RUNS = 10  # below this a share means nothing
_MODEL_MIN_SHARE = 0.5  # most runs never reaching the primary is a fault


def _configured_primaries() -> dict[str, str]:
    """Return ``{agent_id: configured primary model}`` from the manifests.

    Manifests are the source of truth for model assignments. Agents that
    declare no primary are omitted, which makes them invisible to the
    detector — there is nothing to compare against.
    """
    from robothor.engine.config import EngineConfig, load_all_manifests

    primaries: dict[str, str] = {}
    try:
        manifest_dir = EngineConfig.from_env().manifest_dir
        for manifest in load_all_manifests(manifest_dir):
            agent_id = str(manifest.get("id") or "")
            primary = str((manifest.get("model") or {}).get("primary") or "")
            if agent_id and primary:
                primaries[agent_id] = primary
    except Exception as e:  # pragma: no cover - manifest dir missing/unreadable
        logger.debug("could not load configured primaries: %s", e)
    return primaries


def check_primary_model_unreached(
    tenant_id: str = DEFAULT_TENANT,
    window_hours: int = _MODEL_WINDOW_HOURS,
    min_runs: int = _MODEL_MIN_RUNS,
    min_share: float = _MODEL_MIN_SHARE,
    primaries: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return agents whose runs mostly never reached their configured primary.

    A run "reached" its primary when the primary appears in
    ``models_attempted`` — that column lists the models that actually served an
    LLM call, so a run that started on a fallback and recovered still counts as
    reached. Ids are compared through
    :func:`robothor.engine.model_registry.canonical_model_id`: manifests
    configure ``openrouter/xiaomi/mimo-v2.5`` while the provider reports back
    ``xiaomi/mimo-v2.5``, and comparing raw strings would flag every healthy
    run in the fleet.

    Args:
        tenant_id: Tenant whose runs to examine.
        window_hours: How far back to count runs.
        min_runs: Minimum runs in the window before any verdict is possible.
        min_share: Share of runs missing the primary at which to report.
        primaries: ``{agent_id: primary model}``; loaded from the manifests
            when omitted.

    Returns:
        One dict per affected agent: agent_id, primary, total_runs,
        unreached_runs, unreached_share, served ({model: run count}).
    """
    from robothor.db.connection import get_connection
    from robothor.engine.model_registry import canonical_model_id

    if primaries is None:
        primaries = _configured_primaries()
    if not primaries:
        return []

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT agent_id,
                   model_used,
                   COALESCE(models_attempted, ARRAY[]::TEXT[]) AS models_attempted,
                   COUNT(*) AS runs
            FROM agent_runs
            WHERE tenant_id = %(tenant_id)s
              AND started_at > NOW() - make_interval(hours => %(hours)s)
              AND status IN ('completed', 'failed', 'timeout')
              AND model_used IS NOT NULL
            GROUP BY agent_id, model_used, models_attempted
            """,
            {"tenant_id": tenant_id, "hours": window_hours},
        )
        rows = [dict(r) for r in cur.fetchall()]

    totals: dict[str, int] = {}
    unreached: dict[str, int] = {}
    served: dict[str, dict[str, int]] = {}
    for r in rows:
        agent_id = str(r["agent_id"])
        primary = primaries.get(agent_id)
        if not primary:
            continue
        runs = int(r["runs"] or 0)
        model_used = str(r["model_used"] or "")
        attempted = [str(m) for m in (r["models_attempted"] or [])] or [model_used]
        canonical_attempted = {canonical_model_id(m) for m in attempted}
        totals[agent_id] = totals.get(agent_id, 0) + runs
        served.setdefault(agent_id, {})
        served[agent_id][model_used] = served[agent_id].get(model_used, 0) + runs
        if canonical_model_id(primary) not in canonical_attempted:
            unreached[agent_id] = unreached.get(agent_id, 0) + runs

    out: list[dict[str, Any]] = []
    for agent_id, total in totals.items():
        missed = unreached.get(agent_id, 0)
        if total < min_runs or missed / total < min_share:
            continue
        out.append(
            {
                "agent_id": agent_id,
                "primary": primaries[agent_id],
                "total_runs": total,
                "unreached_runs": missed,
                "unreached_share": round(missed / total, 3),
                "served": dict(
                    sorted(served[agent_id].items(), key=lambda kv: -kv[1]),
                ),
            }
        )
    out.sort(key=lambda a: (-a["unreached_share"], a["agent_id"]))
    return out


async def primary_model_unreached_detector(tenant_id: str = DEFAULT_TENANT) -> int:
    """Alert when agents are mostly running on a fallback model."""
    if not detectors_enabled():
        return 0
    fired = 0
    try:
        affected = check_primary_model_unreached(tenant_id=tenant_id)
    except Exception as e:
        logger.debug("primary_model_unreached_detector query failed: %s", e)
        return 0
    from robothor.engine.alerts import alert

    window_days = _MODEL_WINDOW_HOURS // 24
    for a in affected:
        agent_id = str(a["agent_id"])
        primary = str(a["primary"])
        if not _should_fire(f"model_unreached:{agent_id}:{primary}", _SLOW_DEDUP_TTL_SECONDS):
            continue
        served = ", ".join(f"{model}={count}" for model, count in a["served"].items())
        body = (
            f"{agent_id}: {a['unreached_runs']}/{a['total_runs']} runs "
            f"({a['unreached_share'] * 100:.0f}%) never reached primary {primary} "
            f"in the last {window_days}d.\n"
            f"served by: {served}"
        )
        if not await alert("warning", f"Primary model unreached: {agent_id}", body):
            logger.warning("Alert delivery failed for model_unreached:%s", agent_id)
        fired += 1
    return fired
