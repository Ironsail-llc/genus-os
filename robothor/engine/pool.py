"""Fleet-wide pool manager for agent run admission control.

Tracks all active runs (cron, hook, interactive, sub-agent) and enforces
fleet-wide concurrency and hourly cost caps.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ActiveRun:
    """Metadata for a currently-running agent."""

    run_id: str
    agent_id: str
    started_at: float  # monotonic clock
    cost_usd: float = 0.0


@dataclass
class CostRecord:
    """A completed run's cost for hourly window tracking."""

    agent_id: str
    cost_usd: float
    completed_at: float  # monotonic clock


class Priority(StrEnum):
    """How a run competes for a slot.

    INTERACTIVE bypasses admission entirely — a human is waiting, and queueing
    them behind a nightly CRM sweep is the inversion this exists to prevent.
    CRITICAL may take a reserved slot; BACKGROUND may not.
    """

    INTERACTIVE = "interactive"
    CRITICAL = "critical"
    BACKGROUND = "background"


class FleetPool:
    """Fleet-wide admission control for agent runs.

    Provides two control surfaces:
    - **Concurrency**: limits how many agent runs can execute simultaneously
    - **Hourly cost cap**: limits total fleet spend per rolling hour

    Thread-safe — the engine scheduler and async runners may call from
    different threads.
    """

    def __init__(
        self,
        max_concurrent: int = 3,
        hourly_cost_cap_usd: float = 5.0,
        reserved_slots: int = 0,
    ) -> None:
        self._max_concurrent = max_concurrent
        self._hourly_cost_cap_usd = hourly_cost_cap_usd
        # Slots BACKGROUND work may not occupy. A cap alone still lets nightly
        # sweeps fill every slot and leave an operator's message third in line;
        # holding one back makes that structurally impossible. Default 0 keeps
        # existing behaviour byte-for-byte.
        self._reserved_slots = reserved_slots
        self._active: dict[str, ActiveRun] = {}  # run_id → ActiveRun
        self._cost_history: list[CostRecord] = []
        self._lock = Lock()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    @property
    def hourly_cost(self) -> float:
        """Total cost in the last rolling hour."""
        with self._lock:
            return self._hourly_cost_total()

    def can_start(self, agent_id: str, priority: Priority = Priority.CRITICAL) -> tuple[bool, str]:
        """Check if a new run can start.

        Returns (allowed, reason). If not allowed, reason explains why.

        Default priority is CRITICAL so every existing caller keeps its current
        behaviour: it competes for the whole pool and ignores the reservation.
        """
        if priority is Priority.INTERACTIVE:
            # Never queued. Local inference has its own backpressure
            # (OLLAMA_MAX_QUEUE, LOCAL_CAPACITY_RETRIES) to absorb the overshoot;
            # a person waiting on an answer does not.
            return True, ""

        with self._lock:
            limit = self._max_concurrent
            if priority is Priority.BACKGROUND and self._reserved_slots > 0:
                limit = max(0, self._max_concurrent - self._reserved_slots)
                if len(self._active) >= limit:
                    return False, (
                        f"Background run refused: {len(self._active)}/{self._max_concurrent} "
                        f"slots used and {self._reserved_slots} reserved for interactive work"
                    )
            # Concurrency check
            if len(self._active) >= limit:
                return False, (
                    f"Fleet at capacity: {len(self._active)}/{self._max_concurrent} concurrent runs"
                )

            # Hourly cost check
            if self._hourly_cost_cap_usd > 0:
                current = self._hourly_cost_total()
                if current >= self._hourly_cost_cap_usd:
                    return False, (
                        f"Hourly cost cap reached: ${current:.2f}/${self._hourly_cost_cap_usd:.2f}"
                    )

            return True, ""

    def register_run(self, run_id: str, agent_id: str) -> None:
        """Register a new active run."""
        with self._lock:
            self._active[run_id] = ActiveRun(
                run_id=run_id,
                agent_id=agent_id,
                started_at=time.monotonic(),
            )

    def complete_run(self, run_id: str, cost_usd: float = 0.0) -> None:
        """Mark a run as completed and record its cost."""
        with self._lock:
            run = self._active.pop(run_id, None)
            if run:
                self._cost_history.append(
                    CostRecord(
                        agent_id=run.agent_id,
                        cost_usd=cost_usd,
                        completed_at=time.monotonic(),
                    )
                )
            self._prune_cost_history()

    def set_limits(
        self, max_concurrent: int | None = None, reserved_slots: int | None = None
    ) -> None:
        """Retune the live pool without a restart.

        The right limit is not a constant: it is the device's inference
        capacity, which changes the moment the fleet falls back from a cloud
        provider to a single local GPU. Waiting for a restart to apply that is
        waiting through the outage.
        """
        with self._lock:
            if max_concurrent is not None:
                self._max_concurrent = max_concurrent
            if reserved_slots is not None:
                self._reserved_slots = reserved_slots

    def update_cost(self, run_id: str, cost_usd: float) -> None:
        """Update the running cost of an active run."""
        with self._lock:
            if run_id in self._active:
                self._active[run_id].cost_usd = cost_usd

    def stats(self) -> dict[str, Any]:
        """Return pool statistics."""
        with self._lock:
            return {
                "active_runs": len(self._active),
                "max_concurrent": self._max_concurrent,
                "hourly_cost_usd": round(self._hourly_cost_total(), 4),
                "hourly_cost_cap_usd": self._hourly_cost_cap_usd,
                "active_agents": [
                    {"run_id": r.run_id, "agent_id": r.agent_id, "cost_usd": r.cost_usd}
                    for r in self._active.values()
                ],
            }

    def _hourly_cost_total(self) -> float:
        """Sum costs from the last hour. Must be called under lock."""
        self._prune_cost_history()
        completed_cost = sum(r.cost_usd for r in self._cost_history)
        active_cost = sum(r.cost_usd for r in self._active.values())
        return completed_cost + active_cost

    def _prune_cost_history(self) -> None:
        """Remove cost records older than 1 hour. Must be called under lock."""
        cutoff = time.monotonic() - 3600
        self._cost_history = [r for r in self._cost_history if r.completed_at > cutoff]


# Module-level singleton — initialized by daemon
_fleet_pool: FleetPool | None = None


def get_fleet_pool() -> FleetPool | None:
    """Get the fleet pool singleton."""
    return _fleet_pool


def init_fleet_pool(
    max_concurrent: int = 3,
    hourly_cost_cap_usd: float = 5.0,
    reserved_slots: int = 0,
) -> FleetPool:
    """Initialize the fleet pool singleton."""
    global _fleet_pool
    _fleet_pool = FleetPool(
        max_concurrent=max_concurrent,
        hourly_cost_cap_usd=hourly_cost_cap_usd,
        reserved_slots=reserved_slots,
    )
    return _fleet_pool
