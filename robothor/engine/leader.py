"""Leader election for the scheduler (Wave-2, W2-5 — HA).

Exactly one engine replica holds the scheduler/heartbeat leadership lease so cron
jobs fire once across replicas. Built on ``redis_lease``. When HA is off
(``ROBOTHOR_HA_LEADER_ENABLED`` unset — the single-node default), ``is_leader()``
is always True so the scheduler runs normally.

Dedup (redis-backed) + the FleetPool are the real correctness boundary; this
lease is an optimization that avoids redundant fires, not the safety guard.
"""

from __future__ import annotations

import asyncio
import logging
import os

from robothor.engine import redis_lease

logger = logging.getLogger(__name__)

LEADER_KEY = "robothor:leader:scheduler"
_LEASE_TTL_MS = 30_000  # 30s; renew at ttl/3


def ha_leader_enabled() -> bool:
    return os.environ.get("ROBOTHOR_HA_LEADER_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


class LeaderElector:
    """Acquires + renews the scheduler leadership lease in a background loop."""

    def __init__(self, key: str = LEADER_KEY, ttl_ms: int = _LEASE_TTL_MS) -> None:
        self._key = key
        self._ttl = ttl_ms
        self._owner: str | None = None
        # Single-node (HA off) is always leader; HA starts as follower until acquired.
        self._is_leader = not ha_leader_enabled()
        self._stop = False
        self._consec_errors = 0

    def is_leader(self) -> bool:
        return self._is_leader

    async def _tick(self) -> None:
        """One acquire/renew step (separated for testability)."""
        if self._owner is None:
            token = await asyncio.to_thread(redis_lease.acquire, self._key, self._ttl)
            if token:
                self._owner = token
                if not self._is_leader:
                    logger.info("Acquired scheduler leadership")
                self._is_leader = True
            else:
                self._is_leader = False
        else:
            ok = await asyncio.to_thread(redis_lease.renew, self._key, self._owner, self._ttl)
            if not ok:
                logger.warning("Lost scheduler leadership")
                self._owner = None
                self._is_leader = False

    async def run(self) -> None:
        """Background leadership loop. Always-leader when HA is off.

        Must NOT return while the daemon is alive — the daemon shuts down when
        ANY of its tasks completes (FIRST_COMPLETED), so this loop stays alive
        even when HA is off (it just holds leadership and sleeps).
        """
        if not ha_leader_enabled():
            self._is_leader = True
            while not self._stop:
                await asyncio.sleep(3600)
            return
        while not self._stop:
            try:
                await self._tick()
                self._consec_errors = 0
            except Exception as e:
                # A transient error keeps current state (never flip a follower to
                # leader on error). But if we're the leader and can't reach Redis
                # for longer than the lease TTL, our lease has expired and another
                # replica may have taken over — demote to avoid a two-leader split
                # brain. Renews happen every TTL/3, so 3 consecutive failures ≈ TTL.
                self._consec_errors += 1
                logger.warning(
                    "Leader election tick error (%d consecutive): %s", self._consec_errors, e
                )
                if self._is_leader and self._owner is not None and self._consec_errors >= 3:
                    logger.warning(
                        "Demoting from leader: %d consecutive errors exceed lease TTL",
                        self._consec_errors,
                    )
                    self._is_leader = False
                    self._owner = None
            await asyncio.sleep(max(1, self._ttl // 3 // 1000))

    async def stop(self) -> None:
        self._stop = True
        if self._owner is not None:
            try:
                await asyncio.to_thread(redis_lease.release, self._key, self._owner)
            except Exception as e:
                logger.debug("Leader release failed: %s", e)
            self._owner = None
        self._is_leader = not ha_leader_enabled()


# ── Module singleton ──────────────────────────────────────────────────
_elector: LeaderElector | None = None


def get_elector() -> LeaderElector | None:
    return _elector


def set_elector(elector: LeaderElector | None) -> None:
    global _elector
    _elector = elector


def is_leader() -> bool:
    """True if this replica may run scheduled jobs (always True when HA is off)."""
    elec = _elector
    if elec is None:
        return not ha_leader_enabled()
    return elec.is_leader()
