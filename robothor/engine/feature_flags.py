"""Feature flags for the Tier 1-4 upgrade rollout.

Each rip in the upgrade plan is gated by an environment variable
``ROBOTHOR_RIP_<N>_ENABLED``. The operator can toggle a rip without
shipping a new release::

    systemctl set-environment ROBOTHOR_RIP_1_ENABLED=1
    systemctl restart robothor-engine

The global panic switch ``ROBOTHOR_DISABLE_ALL_RIPS=1`` forces every
flag off regardless of per-rip env vars. Use when something is wrong
and you need every new behavior dark immediately.

The trajectory rate ``ROBOTHOR_TRAJECTORY_SAMPLE`` is a float (0.0-1.0)
rather than a bool: it controls the fraction of runs whose transcripts
are written to disk.
"""

from __future__ import annotations

import os
from typing import Literal

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

Rip7Mode = Literal["off", "observe", "alert", "enforce"]
_VALID_RIP_7_MODES = frozenset(("observe", "alert", "enforce"))

SymbolicMode = Literal["off", "observe", "enforce"]
_VALID_SYMBOLIC_MODES = frozenset(("observe", "enforce"))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUE_VALUES


def _disabled_all() -> bool:
    return _env_bool("ROBOTHOR_DISABLE_ALL_RIPS")


def is_rip_enabled(rip_number: int) -> bool:
    """Return True iff rip N's behavior should be active.

    Reads ``ROBOTHOR_RIP_<N>_ENABLED``; default off. Returns False
    unconditionally when ``ROBOTHOR_DISABLE_ALL_RIPS=1``.
    """
    if _disabled_all():
        return False
    return _env_bool(f"ROBOTHOR_RIP_{rip_number}_ENABLED")


def trajectory_sample_rate() -> float:
    """Fraction of runs whose transcripts should be persisted to disk.

    Reads ``ROBOTHOR_TRAJECTORY_SAMPLE``; clamped to [0.0, 1.0]; default 0.0.
    Returns 0.0 unconditionally when ``ROBOTHOR_DISABLE_ALL_RIPS=1``.
    """
    if _disabled_all():
        return 0.0
    raw = os.environ.get("ROBOTHOR_TRAJECTORY_SAMPLE", "").strip()
    if not raw:
        return 0.0
    try:
        rate = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, min(1.0, rate))


def rip_7_enforcement_mode() -> Rip7Mode:
    """Return the drift-detector enforcement mode for memory_facts (Rip 7).

    Returns ``"off"`` when Rip 7 is disabled (or the global panic flag
    is set). Otherwise reads ``ROBOTHOR_RIP_7_MODE`` and returns one of
    ``"observe"`` (default — log only, allow write), ``"alert"`` (log
    + notify operator, allow write), or ``"enforce"`` (audit-snapshot
    and refuse the write).

    The plan rolls this out as observe → alert → enforce, with operator
    inspection of the audit table at each boundary.
    """
    if _disabled_all() or not _env_bool("ROBOTHOR_RIP_7_ENABLED"):
        return "off"
    raw = os.environ.get("ROBOTHOR_RIP_7_MODE", "observe").strip().lower()
    if raw in _VALID_RIP_7_MODES:
        return raw  # type: ignore[return-value]
    return "observe"


def deferred_tools_enabled() -> bool:
    """Return True iff deferred/searchable tool loading (Rip 16 / G4) is active.

    When on, broad-access agents are advertised only a small CORE_TOOLS set plus
    the tool_search/tool_describe/tool_call meta-tools; the rest of their allowed
    tools load on demand. Off by default; gated by ``ROBOTHOR_RIP_16_ENABLED``.
    """
    return is_rip_enabled(16)


def deferred_tools_threshold() -> int:
    """Min advertised-tool count above which an agent's set is deferred.

    Agents with curated small toolsets stay fully advertised (no extra
    tool_search round-trip); only broad-access agents (e.g. main) defer.
    Reads ``ROBOTHOR_DEFERRED_TOOLS_THRESHOLD`` (default 40).
    """
    raw = os.environ.get("ROBOTHOR_DEFERRED_TOOLS_THRESHOLD", "").strip()
    if not raw:
        return 40
    try:
        return max(1, int(raw))
    except ValueError:
        return 40


def symbolic_memory_mode() -> SymbolicMode:
    """Return the symbolic-compaction mode for tool logs (Rip 13).

    ``"off"`` when Rip 13 is disabled (or the global panic flag is set).
    Otherwise reads ``ROBOTHOR_RIP_13_MODE``: ``"observe"`` (default — build
    the symbol graph and log the would-be token savings, but leave injected
    context unchanged) or ``"enforce"`` (inject the compact graph in place of
    raw tool output). Rolls out observe → enforce.
    """
    if _disabled_all() or not _env_bool("ROBOTHOR_RIP_13_ENABLED"):
        return "off"
    raw = os.environ.get("ROBOTHOR_RIP_13_MODE", "observe").strip().lower()
    if raw in _VALID_SYMBOLIC_MODES:
        return raw  # type: ignore[return-value]
    return "observe"
