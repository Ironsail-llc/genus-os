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
# Generic observe→alert→enforce rollout ladder, shared by the Wave-1
# hardening flags below. Same shape as ``rip_7_enforcement_mode``.
EnforcementMode = Literal["off", "observe", "alert", "enforce"]
_VALID_ENFORCEMENT_MODES = frozenset(("observe", "alert", "enforce"))


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


def cron_warmup_recall_enabled() -> bool:
    """Give cron/scheduled runs entity-aware recall seeded from the agent goal.

    Interactive (Telegram) warmup already pulls entity facts from the user's
    message; autonomous cron runs do not, so the fleet starts a turn with no
    query-relevant recall and re-derives known context (R2). When on,
    ``build_warmth_preamble`` adds a goal-seeded entity-recall section. Default
    OFF; gated by ``MEMORY_CRON_WARMUP_RECALL``.
    """
    return _env_bool("MEMORY_CRON_WARMUP_RECALL")


def narrow_memory_search_enabled() -> bool:
    """Make the default search_memory tool path fact-only (no auto fan-out).

    The default (RIP-15-off) path hard-codes expand_entities/include_insights/
    include_episodes=True on every call, so a narrow lookup pays for a full
    fan-out (R2, token waste). When on, those default to False (facts only).
    Default OFF (observe token delta first); gated by ``MEMORY_NARROW_SEARCH``.
    """
    return _env_bool("MEMORY_NARROW_SEARCH")


def compaction_hardening_enabled() -> bool:
    """Return True iff compaction hardening (Rip 18 / G7) is active.

    When on, compaction runs a cheap LLM-free pre-pass before summarizing:
    dedup identical tool results (keep the newest full copy) and strip
    historical media (base64 images) outside the protected recent tail — both
    pure token wins with no information loss the agent can't re-fetch. Off by
    default; gated by ``ROBOTHOR_RIP_18_ENABLED``.
    """
    return is_rip_enabled(18)


def catalog_backed_models_enabled() -> bool:
    """Return True iff the catalog-backed model registry (Rip 17 / G6) is active.

    When on: (1) litellm pricing is registered from the single ``_MODEL_REGISTRY``
    source instead of a separate hand-maintained dict (ends drift), and (2)
    unknown models fall back to litellm's bundled catalog (accurate window +
    pricing for hundreds of models) instead of a flat 128K/8K guess. Off by
    default; gated by ``ROBOTHOR_RIP_17_ENABLED``.
    """
    return is_rip_enabled(17)


def goal_judge_enabled() -> bool:
    """Return True iff the goal-judge (self-improvement Phase 1) is active.

    When on, the ``judge_run`` tool grades an agent's recent runs against real
    outcome signals (declared goal, run trace, operator words, obstacles) and
    writes ``agent_reviews`` rows with ``reviewer_type='judge'`` that become the
    spine of the achievement score. Off by default; gated by
    ``ROBOTHOR_JUDGE_ENABLED`` (not a numbered rip — a distinct subsystem).
    Forced off by ``ROBOTHOR_DISABLE_ALL_RIPS=1``.
    """
    if _disabled_all():
        return False
    return _env_bool("ROBOTHOR_JUDGE_ENABLED")


def curator_enabled() -> bool:
    """True iff the destructive LLM skill-consolidation curator (Rip 5) is active.

    Gates ONLY the LLM consolidation pass (merge/archive). The non-destructive
    lifecycle (apply_skill_lifecycle: time-based stale/archived state) runs
    unconditionally in the daemon loop. Off by default; gated by
    ``ROBOTHOR_RIP_5_ENABLED``. Forced off by ``ROBOTHOR_DISABLE_ALL_RIPS=1``.
    """
    return is_rip_enabled(5)


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


def _enforcement_mode(enabled_var: str, mode_var: str) -> EnforcementMode:
    """Generic observe→alert→enforce ladder gated on two env vars.

    Returns ``"off"`` when the global panic flag is set or ``enabled_var``
    is falsy. Otherwise reads ``mode_var`` and returns ``"observe"``
    (default — compute the verdict, log it, but DO NOT act), ``"alert"``
    (observe + notify the operator), or ``"enforce"`` (apply the verdict).

    The default of ``observe`` plus a behavior-preserving default elsewhere
    means flipping ``enabled_var`` on is a no-op until ``mode_var`` is
    promoted, and rollback is instant.
    """
    if _disabled_all() or not _env_bool(enabled_var):
        return "off"
    raw = os.environ.get(mode_var, "observe").strip().lower()
    if raw in _VALID_ENFORCEMENT_MODES:
        return raw  # type: ignore[return-value]
    return "observe"


def sandbox_default_mode() -> EnforcementMode:
    """Rollout mode for defaulting exec-holding agents into the Docker sandbox.

    Gated on ``ROBOTHOR_SANDBOX_DEFAULT_ENABLED`` + ``ROBOTHOR_SANDBOX_DEFAULT_MODE``.
    ``observe`` logs which agents/runs WOULD be sandboxed (running on host as
    today); ``enforce`` sets ``sandbox=docker`` for exec-holding agents that
    have not opted out via ``sandbox: host``.
    """
    return _enforcement_mode("ROBOTHOR_SANDBOX_DEFAULT_ENABLED", "ROBOTHOR_SANDBOX_DEFAULT_MODE")


def rbac_enforcement_mode() -> EnforcementMode:
    """Rollout mode for RBAC over system/cron/hook runs.

    Gated on ``ROBOTHOR_RBAC_ENABLED`` + ``ROBOTHOR_RBAC_MODE``. ``observe``
    computes the permission verdict and logs would-denies but returns allow;
    ``enforce`` actually denies. The default ``service`` role is allow-all, so
    observe should surface zero would-denies unless a role was deliberately
    tightened.
    """
    return _enforcement_mode("ROBOTHOR_RBAC_ENABLED", "ROBOTHOR_RBAC_MODE")


def approval_mode() -> EnforcementMode:
    """Rollout mode for fail-closed human-approval escalation.

    Gated on ``ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED`` + ``ROBOTHOR_APPROVAL_MODE``.
    ``observe`` logs escalations that WOULD be denied (no/declining manager) but
    proceeds (auto-approves, as today); ``enforce`` denies the tool call when no
    approver is reachable.
    """
    return _enforcement_mode("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", "ROBOTHOR_APPROVAL_MODE")


def exec_allowlist_mode() -> EnforcementMode:
    """Rollout mode for rejecting shell-chaining metacharacters in allowlisted exec.

    Gated on ``ROBOTHOR_EXEC_ALLOWLIST_STRICT_ENABLED`` + ``ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE``.
    ``observe`` logs commands that WOULD be blocked for containing shell control
    characters (``;`` ``|`` ``&`` ``<`` ``>`` ``$(`` backtick) that let an
    attacker chain past an allowlisted prefix, but allows them (legacy behavior);
    ``enforce`` blocks them. Default off preserves today's behavior.
    """
    return _enforcement_mode(
        "ROBOTHOR_EXEC_ALLOWLIST_STRICT_ENABLED", "ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE"
    )


def injection_scan_mode() -> EnforcementMode:
    """Rollout mode for prompt-injection scanning of assembled system-run prompts.

    Gated on ``ROBOTHOR_INJECTION_SCAN_ENABLED`` + ``ROBOTHOR_INJECTION_SCAN_MODE``.
    ``observe``/``alert`` log when an assembled cron/hook prompt (incl. recalled
    memory + skills) matches an injection signal but run anyway; ``enforce``
    aborts the run. Default off.
    """
    return _enforcement_mode("ROBOTHOR_INJECTION_SCAN_ENABLED", "ROBOTHOR_INJECTION_SCAN_MODE")
