"""Config validation — schema-based checks for merged agent manifests.

Fail-open: returns warning strings but never raises. Catches typos, invalid
values, and out-of-range numbers before they become runtime surprises.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ── Known v2 keys (to catch typos) ──────────────────────────────────

_KNOWN_V2_KEYS = frozenset(
    {
        "can_spawn_agents",
        "max_nesting_depth",
        "sub_agent_max_iterations",
        "sub_agent_timeout_seconds",
        "max_concurrent_spawns",
        "max_spawn_batch",
        "mcp_servers",
        "error_feedback",
        "planning_enabled",
        "planning_model",
        "scratchpad_enabled",
        "guardrails",
        "guardrails_opt_out",
        "exec_allowlist",
        "write_path_allowlist",
        "checkpoint_enabled",
        "verification_enabled",
        "verification_prompt",
        "difficulty_class",
        "lifecycle_hooks",
        "sandbox",
        "eager_tool_compression",
        "tool_offload_threshold",
        "safety_cap",
        "continuous",
        "progress_report_interval",
        "max_cost_usd",
        "hard_budget",
        "human_approval_tools",
        "human_approval_timeout",
        "todo_list_enabled",
    }
)

_KNOWN_GUARDRAILS = frozenset(
    {
        "no_destructive_writes",
        "no_sensitive_data",
        "rate_limit",
        "no_external_http",
        "no_main_branch_push",
        "exec_allowlist",
        "write_path_restrict",
        "desktop_safety",
        "human_approval",
        "requires_human_task_closure",
        "recurring_meeting_proposal_required",
    }
)

_KNOWN_DIFFICULTY_CLASSES = frozenset({"", "simple", "moderate", "complex"})

_KNOWN_SANDBOX_MODES = frozenset({"local", "docker"})

_KNOWN_DELIVERY_MODES = frozenset({"none", "announce", "summary", "full"})

_KNOWN_SESSION_TARGETS = frozenset({"isolated", "persistent"})

# Simple cron expression check — 5 or 6 space-separated fields
_CRON_RE = re.compile(r"^[\d\*\/\-\,\?\#LW\s]+$")


def validate_manifest(data: dict[str, Any]) -> list[str]:
    """Validate a merged manifest dict. Returns list of warning strings (empty = valid)."""
    warnings: list[str] = []

    # Required fields
    if not data.get("id"):
        warnings.append("Missing required field: id")

    # Schedule ranges
    schedule = data.get("schedule", {})
    if isinstance(schedule, dict):
        _check_range(warnings, schedule, "timeout_seconds", 0, 86400)
        _check_range(warnings, schedule, "max_iterations", 1, 10000)
        _check_range(warnings, schedule, "safety_cap", 1, 10000)
        _check_range(warnings, schedule, "stall_timeout_seconds", 0, 86400)
        cron = schedule.get("cron", "")
        if cron and not _CRON_RE.match(cron):
            warnings.append(f"Suspicious cron expression: {cron!r}")

    # Delivery mode
    delivery = data.get("delivery", {})
    if isinstance(delivery, dict):
        mode = delivery.get("mode", "none")
        if mode not in _KNOWN_DELIVERY_MODES:
            warnings.append(
                f"Unknown delivery mode: {mode!r} (expected one of {sorted(_KNOWN_DELIVERY_MODES)})"
            )

    # Session target
    if isinstance(schedule, dict):
        target = schedule.get("session_target", "isolated")
        if target not in _KNOWN_SESSION_TARGETS:
            warnings.append(f"Unknown session_target: {target!r}")

    # v2 block
    v2 = data.get("v2", {})
    if isinstance(v2, dict):
        # Unknown v2 keys (typo detection)
        warnings.extend(
            f"Unknown v2 key: {key!r} — possible typo?" for key in v2 if key not in _KNOWN_V2_KEYS
        )

        # Guardrail names
        warnings.extend(
            f"Unknown guardrail: {g!r}"
            for g in v2.get("guardrails", [])
            if g not in _KNOWN_GUARDRAILS
        )

        # Difficulty class
        dc = v2.get("difficulty_class", "")
        if dc not in _KNOWN_DIFFICULTY_CLASSES:
            warnings.append(f"Unknown difficulty_class: {dc!r}")

        # Sandbox
        sb = v2.get("sandbox", "local")
        if sb not in _KNOWN_SANDBOX_MODES:
            warnings.append(f"Unknown sandbox mode: {sb!r}")

        # Numeric ranges
        _check_range(warnings, v2, "max_nesting_depth", 0, 3)
        _check_range(warnings, v2, "sub_agent_max_iterations", 1, 100)
        _check_range(warnings, v2, "sub_agent_timeout_seconds", 1, 3600)
        _check_range(warnings, v2, "safety_cap", 1, 10000)
        _check_range(warnings, v2, "progress_report_interval", 1, 10000)
        _check_range(warnings, v2, "human_approval_timeout", 10, 3600)

        max_cost = v2.get("max_cost_usd", 0)
        if isinstance(max_cost, (int, float)) and max_cost < 0:
            warnings.append(f"max_cost_usd cannot be negative: {max_cost}")

        # Lifecycle hooks basic structure
        for i, hook in enumerate(v2.get("lifecycle_hooks", [])):
            if isinstance(hook, dict):
                if "event" not in hook:
                    warnings.append(f"lifecycle_hooks[{i}] missing 'event'")
                if "handler_type" not in hook:
                    warnings.append(f"lifecycle_hooks[{i}] missing 'handler_type'")
                if "handler" not in hook:
                    warnings.append(f"lifecycle_hooks[{i}] missing 'handler'")
                ht = hook.get("handler_type", "")
                if ht and ht not in ("python", "command", "http", "agent"):
                    warnings.append(f"lifecycle_hooks[{i}] unknown handler_type: {ht!r}")

    return warnings


def _check_range(
    warnings: list[str],
    data: dict[str, Any],
    key: str,
    min_val: int | float,
    max_val: int | float,
) -> None:
    """Check a numeric field is within range, if present."""
    if key not in data:
        return
    val = data[key]
    if not isinstance(val, (int, float)):
        warnings.append(f"{key} should be numeric, got {type(val).__name__}")
        return
    if val < min_val or val > max_val:
        warnings.append(f"{key}={val} is outside expected range [{min_val}, {max_val}]")
