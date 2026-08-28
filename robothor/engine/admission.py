"""Fleet admission: does this run get a slot right now?

Extracted from the scheduler rather than growing it past its decomposition
ratchet. It answers one question, and it is the question `FleetPool` was
written to answer and was never asked.

`pool.py` has shipped with can_start/register_run/complete_run, a full test
suite, and a daemon that initialises it and LOGS the cap it enforces — with no
production caller, for its whole existence. `leader.py:8` asserts "Dedup and
the FleetPool are the real correctness boundary"; only half of that was true.
Measured 2026-08-27: 12 concurrent runs against a configured cap of 3, on a GPU
serving OLLAMA_NUM_PARALLEL=2, with the operator's Telegram turns queued third
behind nightly sweeps (1.2 min idle vs 17.9 min contended).

Everything here fails OPEN. A missing pool, an unloadable manifest or a raised
exception must ADMIT: a scheduler that refuses work because a singleton is
absent is worse than one that refuses nothing.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _pool() -> Any:
    """The live pool, or None when the daemon has not initialised one."""
    try:
        from robothor.engine.pool import get_fleet_pool

        return get_fleet_pool()
    except Exception:  # noqa: BLE001 - admission is a guard, not a dependency
        return None


def admission_mode() -> str:
    """The rollout mode for this gate. Indirected so tests can drive it."""
    try:
        from robothor.engine.feature_flags import execution_mode_admission_mode

        return execution_mode_admission_mode()
    except Exception:  # noqa: BLE001 - an unreadable flag must not refuse work
        return "off"


def _record_deferral(agent_id: str, reason: str, mode: str, priority: str) -> None:
    """Leave a row saying the gate fired. Delegated so this module stays about
    the verdict; see `admission_evidence` for why the row matters."""
    from robothor.engine.admission_evidence import record_deferral

    record_deferral(agent_id, reason, mode, priority)


def admit(agent_id: str, agent_config: Any, engine_config: Any = None) -> bool:
    """Ask for a slot. False means skip this tick.

    Deferral is one omission, not a subsystem: the caller skips WITHOUT
    advancing the schedule's last_run_at, so the existing catch_up /
    stale_after_minutes machinery treats it as a missed fire and coalesces it.
    A deferred agent is retried, not lost. No new queue, no new table.
    """
    mode = admission_mode()
    if mode == "off":
        return True

    pool = _pool()
    if pool is None:
        return True
    try:
        from robothor.engine.agent_priority import classify
        from robothor.engine.models import TriggerType

        priority = classify(agent_id, TriggerType.CRON, agent_config, engine_config)
        allowed, reason = pool.can_start(agent_id, priority=priority)
    except Exception:  # noqa: BLE001
        logger.exception("Admission check failed for %s — admitting", agent_id)
        return True
    if allowed:
        return True

    priority_label = getattr(priority, "value", str(priority))
    logger.warning("Deferring %s (%s) [%s]: %s", agent_id, priority_label, mode, reason)
    try:
        _record_deferral(agent_id, reason, mode, priority_label)
    except Exception:  # noqa: BLE001 - evidence is telemetry, not a verdict
        logger.debug("Could not record admission deferral for %s", agent_id, exc_info=True)

    return mode != "enforce"


def register(run_key: str, agent_id: str) -> None:
    """Take the slot. Pairs with `complete` in a finally."""
    pool = _pool()
    if pool is not None:
        pool.register_run(run_key, agent_id)


def complete(run_key: str, cost_usd: float = 0.0) -> None:
    """Release the slot. A slot leaked on an exception is how an admission
    control becomes the outage it was meant to prevent."""
    pool = _pool()
    if pool is not None:
        pool.complete_run(run_key, cost_usd=cost_usd)
