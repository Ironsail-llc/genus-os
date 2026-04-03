"""autoDream — opportunistic memory consolidation triggered by idle detection.

Wraps existing lifecycle.py functions into an orchestrated consolidation pass.
Runs when the engine is idle (no active agent runs) or after a stall timeout.

Modes:
    idle       — standard consolidation during daytime idle gaps
    post_stall — cleanup after a stalled run times out
    deep       — full lifecycle maintenance during quiet hours (10 PM–6 AM)
    scheduled  — explicitly triggered (e.g., by proactive-check agent)

Usage (from daemon.py autodream loop):
    from robothor.engine.autodream import run_autodream, is_cooled_down
    if is_cooled_down() and not running_agents():
        await run_autodream(mode="idle")
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from robothor.memory.lifecycle import (
    discover_cross_domain_insights,
    prune_low_quality_facts,
    run_intraday_consolidation,
    run_lifecycle_maintenance,
)

logger = logging.getLogger(__name__)

# Minimum seconds between autoDream runs (default 30 min).
COOLDOWN_SECONDS = int(os.environ.get("AUTODREAM_COOLDOWN_SECONDS", "1800"))

# Quiet hours: deep mode runs full lifecycle instead of lightweight pass.
QUIET_HOUR_START = 22  # 10 PM ET
QUIET_HOUR_END = 6  # 6 AM ET


def _is_quiet_hours() -> bool:
    """Check if current time is within quiet hours (ET)."""
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = datetime.now(UTC)
    return now.hour >= QUIET_HOUR_START or now.hour < QUIET_HOUR_END


_LOCK_KEY = "robothor:autodream:lock"
_LAST_RUN_KEY = "robothor:autodream:last_run"


def _get_last_run_ts() -> float | None:
    """Read the last autoDream run timestamp from Redis. Returns epoch or None."""
    try:
        from robothor.events.bus import _get_redis

        r = _get_redis()
        if r is None:
            return None
        val = r.get(_LAST_RUN_KEY)
        return float(val) if val else None
    except Exception:
        return None


def _set_last_run_ts() -> None:
    """Write the current timestamp as the last autoDream run time."""
    try:
        from robothor.events.bus import _get_redis

        r = _get_redis()
        if r is None:
            return
        r.set(_LAST_RUN_KEY, str(time.time()), ex=86400)
    except Exception as e:
        logger.debug("Failed to set autoDream timestamp: %s", e)


def is_cooled_down() -> bool:
    """Check whether enough time has passed since the last autoDream run."""
    last = _get_last_run_ts()
    if last is None:
        return True
    return (time.time() - last) >= COOLDOWN_SECONDS


def try_acquire_lock(run_id: str) -> bool:
    """Acquire a distributed autoDream lock via Redis SET NX.

    Prevents overlapping runs across multiple daemon instances.
    Lock auto-expires after COOLDOWN_SECONDS to prevent deadlocks.
    """
    try:
        from robothor.events.bus import _get_redis

        r = _get_redis()
        if r is None:
            return True  # No Redis = single instance, allow
        acquired: bool = bool(r.set(_LOCK_KEY, run_id, nx=True, ex=COOLDOWN_SECONDS))
        if not acquired:
            logger.debug("autoDream lock held by another instance")
        return acquired
    except Exception as e:
        logger.debug("autoDream lock acquisition failed: %s", e)
        return True  # Fail open for single-instance setups


def release_lock(run_id: str) -> None:
    """Release the autoDream lock only if we own it (compare-and-delete)."""
    try:
        from robothor.events.bus import _get_redis

        r = _get_redis()
        if r is None:
            return
        current = r.get(_LOCK_KEY)
        if current and current == run_id:
            r.delete(_LOCK_KEY)
    except Exception as e:
        logger.debug("autoDream lock release failed: %s", e)


def _publish_event(event_type: str, data: dict[str, Any]) -> None:
    """Publish an autoDream event to the Redis event bus."""
    try:
        from robothor.events.bus import publish

        publish("system", event_type, data, source="autodream")
    except Exception as e:
        logger.debug("Failed to publish autoDream event: %s", e)


def _record_run(
    run_id: str,
    mode: str,
    started_at: datetime,
    results: dict[str, Any],
    error: str | None = None,
) -> None:
    """Persist autoDream run results to the database."""
    try:
        from robothor.db.connection import get_connection

        duration_ms = int((time.time() - started_at.timestamp()) * 1000)
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO autodream_runs
                    (id, mode, started_at, completed_at, duration_ms,
                     facts_consolidated, facts_pruned, insights_discovered,
                     importance_scores_updated, error_message)
                VALUES (%s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    mode,
                    started_at,
                    duration_ms,
                    results.get("facts_consolidated", 0),
                    results.get("facts_pruned", 0),
                    results.get("insights_discovered", 0),
                    results.get("importance_scores_updated", 0),
                    error,
                ),
            )
            conn.commit()
    except Exception as e:
        logger.warning("Failed to record autoDream run: %s", e)


def _update_memory_block(results: dict[str, Any], mode: str) -> None:
    """Write a summary to the autodream_log memory block."""
    try:
        from robothor.memory.blocks import write_block

        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            f"Last dream: {now} (mode={mode})",
            f"  Consolidated: {results.get('facts_consolidated', 0)} facts",
            f"  Pruned: {results.get('facts_pruned', 0)} facts",
            f"  Insights: {results.get('insights_discovered', 0)} new",
        ]
        if results.get("importance_scores_updated"):
            lines.append(f"  Importance re-scored: {results['importance_scores_updated']}")
        write_block("autodream_log", "\n".join(lines))
    except Exception as e:
        logger.debug("Failed to update autodream_log block: %s", e)


async def run_autodream(mode: str = "idle") -> dict[str, Any]:
    """Run an autoDream memory consolidation pass.

    Args:
        mode: One of 'idle', 'post_stall', 'deep', 'scheduled'.
              'deep' runs full lifecycle maintenance (importance re-scoring).
              Others run lightweight consolidation + pruning + insights.

    Returns:
        Dict with consolidated results and timing.
    """
    run_id = str(uuid.uuid4())

    # Acquire distributed lock to prevent overlapping runs
    if not try_acquire_lock(run_id):
        return {"run_id": run_id, "mode": mode, "skipped": True, "reason": "lock_held"}

    started_at = datetime.now(UTC)
    t0 = time.monotonic()

    # Auto-select deep mode during quiet hours
    if mode == "idle" and _is_quiet_hours():
        mode = "deep"
        logger.info("autoDream: quiet hours detected, upgrading to deep mode")

    logger.info("autoDream starting (mode=%s, run_id=%s)", mode, run_id)

    results: dict[str, Any] = {
        "run_id": run_id,
        "mode": mode,
        "facts_consolidated": 0,
        "facts_pruned": 0,
        "insights_discovered": 0,
        "importance_scores_updated": 0,
    }

    error_msg: str | None = None

    try:
        if mode == "deep":
            # Full lifecycle: importance scoring + decay + prune + consolidate + insights
            maint_results = await run_lifecycle_maintenance()
            results["facts_consolidated"] = maint_results.get("consolidation_groups", 0)
            results["facts_pruned"] = maint_results.get("total_pruned", 0)
            results["insights_discovered"] = len(maint_results.get("insights", []))
            results["importance_scores_updated"] = maint_results.get("facts_scored", 0)
        else:
            # Lightweight: consolidation + pruning + insights (no importance re-scoring)
            # Step 1: Consolidate similar facts
            consol = await run_intraday_consolidation(threshold=3)
            if not consol.get("skipped"):
                results["facts_consolidated"] = consol.get("consolidation_groups", 0)

            # Step 2: Prune low-quality facts
            pruned = await prune_low_quality_facts()
            results["facts_pruned"] = pruned.get("total_pruned", 0)

            # Step 3: Discover cross-domain insights
            insights = await discover_cross_domain_insights(hours_back=72)
            results["insights_discovered"] = len(insights)

    except Exception as e:
        error_msg = str(e)
        logger.exception("autoDream failed (mode=%s): %s", mode, e)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    results["duration_ms"] = elapsed_ms

    # Record, publish, and release lock
    try:
        _set_last_run_ts()
        _record_run(run_id, mode, started_at, results, error=error_msg)
        _update_memory_block(results, mode)
        _publish_event(
            "autodream.complete",
            {
                "run_id": run_id,
                "mode": mode,
                "duration_ms": elapsed_ms,
                "facts_consolidated": results["facts_consolidated"],
                "facts_pruned": results["facts_pruned"],
                "insights_discovered": results["insights_discovered"],
                "error": error_msg,
            },
        )
    finally:
        release_lock(run_id)

    status = "completed" if error_msg is None else "failed"
    logger.info(
        "autoDream %s (mode=%s, %dms): consolidated=%d, pruned=%d, insights=%d",
        status,
        mode,
        elapsed_ms,
        results["facts_consolidated"],
        results["facts_pruned"],
        results["insights_discovered"],
    )

    return results
