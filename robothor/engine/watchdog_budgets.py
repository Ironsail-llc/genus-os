"""Tempo-scaled time budgets: size a run's clocks for the model that answers it.

Extracted here rather than growing ``run_budget`` past its ratchet cap, the
same way the finalization cluster was. It answers one question — how long may
this run be silent, and how long may it live — for a fleet whose models differ
in speed by 3x.

The 2026-08-27 outage is the motivation. Manifest budgets (120s, 180s, 300s)
were all calibrated against cloud latency. When the OpenRouter weekly cap sent
every agent to the local ``ollama_chat/qwen3.8:27b`` tier, those budgets became
shorter than the 600s the engine itself allows a single local call, so the
watchdog began killing runs the LLM layer considered perfectly healthy.

Rather than re-tune 20 manifests on every fleet model change, scale them by the
model's own registered ``ttft_hint_ms`` — metadata that had sat in the registry
unread since it was written, documented as "for interactive routing".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Wall-clock is a BACKSTOP, so its tempo scaling is clamped even when the
#: model is slower still. main's successful local-tier runs average 33.5 min
#: and reach 47.3 min, so 2x the 3600s fleet ceiling covers them with headroom;
#: the local tier's uncapped 3.0 would push the backstop to three hours and
#: turn it into a suggestion. Stall and early-stall take the uncapped factor —
#: they measure silence, not total work.
_WALLCLOCK_TEMPO_CAP = 2.0


@dataclass(frozen=True)
class WatchdogBudgets:
    """The three numbers the stall watchdog runs on, from one derivation."""

    stall: int
    early_stall: int
    hard: int


def chain_for(agent_config: Any) -> list[str]:
    """The models a run may fall through to, primary first."""
    return [
        m
        for m in (
            getattr(agent_config, "model_primary", ""),
            *(getattr(agent_config, "model_fallbacks", None) or []),
        )
        if m
    ]


def effective_wallclock_ceiling(timeout_seconds: int, models: Sequence[str] = ()) -> int:
    """The hard wall-clock bound a run actually gets.

    An agent's own ``timeout_seconds`` when positive; the fleet ceiling when
    the agent declares 0 ("no cap") — nothing runs unbounded. One derivation,
    used by both the watchdog setup and the run loop's self-check, so the two
    can never disagree about what the ceiling is.

    ``models`` is the run's configured chain. When given, the bound is scaled
    by the slowest member's tempo (clamped, see ``_WALLCLOCK_TEMPO_CAP``) so a
    fleet that has fallen back to a local tier is not held to cloud timings.
    Omitted, behaviour is exactly what it was — existing callers are unaffected.
    """
    from robothor.engine.model_registry import chain_tempo_factor
    from robothor.engine.stall_watchdog import _fleet_wallclock_ceiling

    base = timeout_seconds if timeout_seconds > 0 else _fleet_wallclock_ceiling()
    if base <= 0:
        return base  # disabled stays disabled
    factor = min(chain_tempo_factor(models), _WALLCLOCK_TEMPO_CAP) if models else 1.0
    return int(base * factor)


def watchdog_budgets_for(agent_config: Any) -> WatchdogBudgets:
    """Every budget the watchdog needs, scaled for the chain that will serve it.

    The single source for all three numbers. Before this, the runner derived
    the hard ceiling inline while the loop's self-check called
    ``effective_wallclock_ceiling`` — two derivations of one value, which is
    how a loop can kill a run at 3600s while the watchdog believes it has 7200.

    A 0 budget means "disabled" and stays 0: ``_defaults.yaml`` sets
    ``stall_timeout_seconds: 0`` fleet-wide, and scaling that into a live
    timeout would kill every agent on the fleet.
    """
    from robothor.engine.model_registry import chain_tempo_factor

    chain = chain_for(agent_config)
    factor = chain_tempo_factor(chain)

    def _scale(value: int) -> int:
        return int(value * factor) if value > 0 else 0

    return WatchdogBudgets(
        stall=_scale(int(getattr(agent_config, "stall_timeout_seconds", 0) or 0)),
        early_stall=_scale(int(getattr(agent_config, "early_stall_timeout_seconds", 0) or 0)),
        hard=effective_wallclock_ceiling(
            int(getattr(agent_config, "timeout_seconds", 0) or 0), models=chain
        ),
    )
