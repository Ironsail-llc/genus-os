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

import asyncio
import logging
import math
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
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

# Lock TTL must cover the maximum possible run duration, not just the cooldown.
# Observed runs can take 90+ minutes; use 4h as a safe upper bound.
# Configurable via AUTODREAM_LOCK_TTL_SECONDS.
_LOCK_TTL_SECONDS = int(os.environ.get("AUTODREAM_LOCK_TTL_SECONDS", str(4 * 3600)))


# Filesystem fallback for last_run timestamp (survives Redis restarts). Kept
# under the workspace's private .robothor dir — NOT world-writable /tmp, whose
# predictable path let a local attacker pre-plant a symlink and have the daemon
# clobber an arbitrary file / read a spoofed timestamp (CWE-59/377).
def _default_fallback_path() -> str:
    workspace = os.environ.get("ROBOTHOR_WORKSPACE") or str(Path.home() / "robothor")
    return str(Path(workspace) / ".robothor" / "autodream_last_run")


_FALLBACK_PATH = os.environ.get("AUTODREAM_FALLBACK_PATH") or _default_fallback_path()

# Clock-skew tolerance: a stored timestamp more than this far in the future is
# treated as corrupt (clock skew / NTP step-back / a bad write). Rejecting it
# prevents a future value from wedging is_cooled_down() to False forever.
_FUTURE_SKEW_TOLERANCE_SECONDS = int(os.environ.get("AUTODREAM_FUTURE_SKEW_SECONDS", "300"))

# Bound how old the no-TTL fallback file may be before it's ignored on read.
# Slightly larger than the 24h Redis key TTL so the file outlives a Redis
# restart but a multi-day-old value can't poison cooldown/defer decisions.
_FALLBACK_MAX_AGE_SECONDS = int(
    os.environ.get("AUTODREAM_FALLBACK_MAX_AGE_SECONDS", str(25 * 3600))
)

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


def _validate_ts(ts: float) -> bool:
    """Reject corrupt timestamps that would wedge cooldown/staleness logic.

    A non-finite value (NaN / ±inf) or a value implausibly far in the future
    cannot be trusted: NaN/-inf silently break `is_cooled_down()` (a NaN
    comparison is always False; -inf yields an enormous staleness), and a
    future value makes `(now - ts)` negative forever. Treat all of these as
    "no usable timestamp" so callers self-heal.
    """
    if not math.isfinite(ts):
        return False
    return ts <= time.time() + _FUTURE_SKEW_TOLERANCE_SECONDS


def _get_last_run_ts_with_source() -> tuple[float | None, str]:
    """Read the last autoDream run timestamp and report which tier served it.

    Tries Redis first, then the filesystem fallback. Both candidate values are
    run through `_validate_ts`; an invalid value yields ``(None, "invalid")``
    rather than silently poisoning cooldown/staleness. The fallback file is
    also ignored if it is older than ``_FALLBACK_MAX_AGE_SECONDS``.

    Returns ``(epoch_float, source)`` where source is one of
    ``"redis" | "file" | "none" | "invalid"``.
    """
    # Try Redis first.
    try:
        from robothor.events.bus import _get_redis

        r = _get_redis()
        if r is not None:
            val = r.get(_LAST_RUN_KEY)
            if val:
                ts = float(val)
                if not _validate_ts(ts):
                    logger.warning("autoDream Redis timestamp invalid/future (%r) — ignoring", val)
                    return None, "invalid"
                return ts, "redis"
    except Exception:
        pass

    # Filesystem fallback (survives Redis restarts).
    try:
        _fallback = Path(_FALLBACK_PATH)
        if time.time() - _fallback.stat().st_mtime > _FALLBACK_MAX_AGE_SECONDS:
            return None, "none"  # too old to trust — bound the no-TTL file
        with _fallback.open() as f:
            raw = f.read().strip()
        if not raw:
            return None, "none"
        ts = float(raw)
        if not _validate_ts(ts):
            logger.warning("autoDream fallback timestamp invalid/future (%r) — ignoring", raw)
            return None, "invalid"
        return ts, "file"
    except FileNotFoundError:
        return None, "none"
    except Exception as e:
        logger.debug("Failed to read autoDream fallback timestamp: %s", e)
        return None, "none"


def _get_last_run_ts() -> float | None:
    """Read the last autoDream run timestamp (epoch float, or None).

    Thin wrapper over `_get_last_run_ts_with_source` so existing callers keep
    their signature while gaining future/corrupt-value rejection.
    """
    return _get_last_run_ts_with_source()[0]


def _set_last_run_ts() -> None:
    """Write the current timestamp as the last autoDream run time.

    Writes to both Redis and the filesystem fallback so the timestamp
    survives Redis restarts and the watchdog always has something to read.
    """
    ts = str(time.time())

    # Write to Redis
    try:
        from robothor.events.bus import _get_redis

        r = _get_redis()
        if r is not None:
            r.set(_LAST_RUN_KEY, ts, ex=86400)
    except Exception as e:
        logger.debug("Failed to set autoDream Redis timestamp: %s", e)

    # Write filesystem fallback. The file has no TTL, so reads bound its age
    # (_FALLBACK_MAX_AGE_SECONDS) and reject non-finite/future values. A stale
    # *past* value is harmless: it only makes is_cooled_down() return True
    # *sooner* (a run is allowed, which overwrites the value).
    try:
        p = Path(_FALLBACK_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        # O_NOFOLLOW so a symlink at the path is never followed (no arbitrary
        # clobber); O_CREAT|O_TRUNC|O_WRONLY to (re)write our own regular file.
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, ts.encode())
        finally:
            os.close(fd)
    except Exception as e:
        logger.debug("Failed to write autoDream fallback timestamp: %s", e)


def is_cooled_down() -> bool:
    """Check whether enough time has passed since the last autoDream run."""
    last = _get_last_run_ts()
    if last is None:
        return True
    return (time.time() - last) >= COOLDOWN_SECONDS


def try_acquire_lock(run_id: str) -> bool:
    """Acquire a distributed autoDream lock via Redis SET NX.

    Prevents overlapping runs across multiple daemon instances.
    Lock TTL is set to _LOCK_TTL_SECONDS (4h by default) — long enough to
    cover the maximum observed run duration so a slow run can't race with
    a fresh one when the lock expires before the run completes.
    """
    try:
        from robothor.events.bus import _get_redis

        r = _get_redis()
        if r is None:
            return True  # No Redis = single instance, allow
        acquired: bool = bool(r.set(_LOCK_KEY, run_id, nx=True, ex=_LOCK_TTL_SECONDS))
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
        wc = results.get("working_context")
        if wc:
            lines.append(
                f"  working_context refreshed: {wc.get('tasks', 0)} tasks, "
                f"{wc.get('facts', 0)} facts, {wc.get('intents', 0)} intents"
            )
        pr = results.get("preferences")
        if pr:
            lines.append(
                f"  preferences: +{pr.get('new', 0)} new, {pr.get('reinforced', 0)} reinforced"
            )
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

        # Near-duplicate collapse. Runs here rather than on its own timer so it
        # inherits autoDream's Redis lock, cooldown and quiet hours — a second
        # writer to memory_facts outside that lock could interleave with
        # consolidation. Sync function, so off the loop thread.
        #
        # Ladder: off (absent) -> observe (writes candidate audit rows, changes
        # nothing) -> enforce. Observe is deliberately not silent; a soak whose
        # evidence is "no events" cannot tell a working control from an inert
        # one.
        try:
            dechurn_mode = os.environ.get("MEMORY_DECHURN_MODE", "off").strip().lower()
            if dechurn_mode in ("observe", "enforce"):
                # dechurn requires an explicit tenant by design, so resolve it
                # here rather than letting it default. `mode` is the autoDream
                # pass mode, so the ladder value needs its own name.
                from robothor.constants import DEFAULT_TENANT
                from robothor.memory.dechurn import dechurn

                rep = await asyncio.to_thread(
                    dechurn,
                    os.environ.get("ROBOTHOR_TENANT_ID") or DEFAULT_TENANT,
                    dry_run=(dechurn_mode != "enforce"),
                )
                results["dechurn"] = {
                    "mode": dechurn_mode,
                    "candidates": rep.get("near_dup_losers", 0),
                    "deactivated": rep.get("deactivated", 0),
                    "refused": rep.get("refused"),
                }
                if rep.get("refused"):
                    logger.error("autoDream dechurn refused: %s", rep["refused"])
        except Exception as e:  # noqa: BLE001 — hygiene must not fail the pass
            logger.warning("autoDream dechurn failed: %s", e)

        # Step 4: Refresh the live working_context snapshot (replaces stale
        # content with today's open tasks + recent high-signal facts + intents).
        try:
            from robothor.memory.working_context import refresh_working_context

            results["working_context"] = await asyncio.to_thread(refresh_working_context)
        except Exception as e:  # noqa: BLE001 — best-effort hygiene
            logger.warning("autoDream working_context refresh failed: %s", e)

        # Step 5: Mine recent facts for durable operator preferences.
        try:
            from robothor.memory.preferences import extract_preferences_from_facts

            results["preferences"] = await extract_preferences_from_facts(hours_back=168)
        except Exception as e:  # noqa: BLE001 — best-effort hygiene
            logger.warning("autoDream preference extraction failed: %s", e)

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
