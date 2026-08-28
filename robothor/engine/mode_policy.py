"""The ruleset a mode carries: what is scarce, and what to do about it.

Local inference is registered at zero cost per token, which is true about money
and false about everything else. It is paid in heat, in resident memory and in
inference slots -- and those three are exactly what the engine's governors
could not see, so unlimited local work read as free right up until the thermal
guard cut the machine. A mode is therefore not a label; it is which scarcity
applies.

The split is enforced in both directions. The monetary governor is inert in
LOCAL because there is no money to spend, and the thermal governor is inert in
CLOUD because someone else's datacentre is not our heat budget. A policy object
that quietly applied the wrong economics would be worse than none.

``policy_for`` is a pure function of (mode, host profile). CLOUD reproduces
today's constants exactly -- that is what makes this safe to ship ahead of any
gate being enabled.
"""

from __future__ import annotations

from dataclasses import dataclass

from robothor.engine.execution_mode import ExecutionMode
from robothor.engine.host_profile import HostProfile, detect_host_profile

#: Cloud retries on a capacity refusal. Cloud fails fast and rotates the
#: credential; waiting on a provider that is shedding load helps nobody.
CLOUD_CAPACITY_RETRIES = 1

#: Held back from background work so an operator's turn is never third in line
#: behind cron. Applies only where slots are the scarce resource.
LOCAL_INTERACTIVE_RESERVE = 1


@dataclass(frozen=True)
class ModePolicy:
    """What is scarce in this mode, and the limits that follow from it."""

    mode: ExecutionMode
    max_concurrent_runs: int
    reserved_interactive_slots: int
    time_budget_multiplier: float
    monetary_governor: bool
    thermal_governor: bool
    capacity_retries: int
    request_timeout_seconds: int

    @property
    def background_slots(self) -> int:
        """Slots background work may occupy. Never zero: a mode that admits no
        background work at all is a stalled fleet, not a conservative one."""
        return max(1, self.max_concurrent_runs - self.reserved_interactive_slots)

    def describe(self) -> dict[str, object]:
        return {
            "mode": str(self.mode),
            "max_concurrent_runs": self.max_concurrent_runs,
            "reserved_interactive_slots": self.reserved_interactive_slots,
            "background_slots": self.background_slots,
            "time_budget_multiplier": self.time_budget_multiplier,
            "monetary_governor": self.monetary_governor,
            "thermal_governor": self.thermal_governor,
            "capacity_retries": self.capacity_retries,
            "request_timeout_seconds": self.request_timeout_seconds,
        }


def policy_for(
    mode: ExecutionMode,
    host: HostProfile | None = None,
    cloud_max_concurrent: int = 3,
    time_budget_multiplier: float = 1.0,
) -> ModePolicy:
    """Derive the ruleset for ``mode`` on this machine. Pure; safe to call often."""
    from robothor.engine.llm_client import (
        LLM_REQUEST_TIMEOUT,
        LLM_REQUEST_TIMEOUT_OLLAMA,
        LOCAL_CAPACITY_RETRIES,
    )

    if mode is ExecutionMode.CLOUD:
        # Byte-for-byte today's behaviour. The host profile is deliberately
        # ignored here: a one-slot laptop must not throttle cloud fan-out.
        return ModePolicy(
            mode=ExecutionMode.CLOUD,
            max_concurrent_runs=cloud_max_concurrent,
            reserved_interactive_slots=0,
            time_budget_multiplier=1.0,
            monetary_governor=True,
            thermal_governor=False,
            capacity_retries=CLOUD_CAPACITY_RETRIES,
            request_timeout_seconds=LLM_REQUEST_TIMEOUT,
        )

    host = host or detect_host_profile()
    slots = host.inference_slots.value
    if not isinstance(slots, int) or slots < 1:
        slots = 1
    reserve = LOCAL_INTERACTIVE_RESERVE if slots > 1 else 0

    return ModePolicy(
        mode=ExecutionMode.LOCAL,
        max_concurrent_runs=slots,
        reserved_interactive_slots=reserve,
        time_budget_multiplier=time_budget_multiplier,
        monetary_governor=False,
        thermal_governor=bool(host.thermal_sensors.value),
        capacity_retries=LOCAL_CAPACITY_RETRIES,
        request_timeout_seconds=LLM_REQUEST_TIMEOUT_OLLAMA,
    )


def current_policy(cloud_max_concurrent: int = 3) -> ModePolicy:
    """The policy in force right now, from the observed mode and this machine."""
    from robothor.engine.execution_mode import current_mode

    return policy_for(current_mode(), cloud_max_concurrent=cloud_max_concurrent)
