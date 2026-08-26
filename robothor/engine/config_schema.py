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


def _v2_keys_read_by_config() -> frozenset[str]:
    """Every key `config.py` actually reads out of the `v2:` block.

    Derived from the source rather than hand-maintained. The hand-written
    list had drifted three keys — `rate_limit_per_minute`,
    `tool_timeout_seconds` and `human_approval_fail_open` — so a manifest
    that set any of them CORRECTLY was told "possible typo?", while the same
    key in the wrong block said nothing at all. Exactly backwards, and this
    project has now shipped that same drifted-hardcoded-list defect enough
    times to stop writing them by hand.

    Falls back to a static set if the source cannot be read, because a
    validator must never be the reason a manifest fails to load.
    """
    import re
    from pathlib import Path as _Path

    static = frozenset(
        {
            "can_spawn_agents",
            "max_nesting_depth",
            "guardrails",
            "sandbox",
            "rate_limit_per_minute",
        }
    )
    try:
        src = (_Path(__file__).parent / "config.py").read_text(encoding="utf-8")
    except OSError:
        return static
    found = frozenset(re.findall(r"""v2\.get\(\s*['"]([a-z0-9_]+)""", src))
    return found or static


_KNOWN_V2_KEYS = _v2_keys_read_by_config()

# Derived, not duplicated. This used to be a hand-maintained copy of the
# enforcement sets in guardrails.py, and the two drifted in BOTH directions:
# this list was missing inbound_only and no_recent_changelog_reversal (spurious
# "Unknown guardrail" warnings on every boot for agents using them), and it
# carried requires_human_task_closure before the enforcement set did. A
# hand-maintained parallel list is the same defect class as the hardcoded
# alert-name list and the model-switch list — it always drifts.


def _known_guardrails() -> frozenset[str]:
    from robothor.engine.guardrails import _KNOWN_POLICIES

    return _KNOWN_POLICIES


_KNOWN_GUARDRAILS = _known_guardrails()

_KNOWN_DIFFICULTY_CLASSES = frozenset({"", "simple", "moderate", "complex"})

# "host" is the explicit opt-out from sandbox-by-default for host-trusted
# agents — honored by runner._resolve_sandbox_decision and documented in
# docs/agents/schema.yaml. RBAC still gates their tool calls.
_KNOWN_SANDBOX_MODES = frozenset({"local", "docker", "host"})

_KNOWN_DELIVERY_MODES = frozenset({"none", "announce", "summary", "full"})

_KNOWN_SESSION_TARGETS = frozenset({"isolated", "persistent"})

# Simple cron expression check — 5 or 6 space-separated fields
_CRON_RE = re.compile(r"^[\d\*\/\-\,\?\#LW\s]+$")


def _check_last_resort_model(warnings: list[str]) -> None:
    """The model every agent's chain ends in is appended AFTER validation.

    ``_with_last_resort`` (config.py) adds ``ROBOTHOR_LAST_RESORT_MODEL`` to
    every chain inside ``manifest_to_agent_config``, which runs after
    ``validate_manifest``. So the one model the entire fleet's offline tier
    depends on was the one model nothing could check — a typo in robothor.env,
    or an ``ollama rm``, produced a fleet-wide chain ending in fiction with
    zero warnings on any agent. That is the exact failure ``_check_model_block``
    was written to prevent, one layer above where it could see it.
    """
    import os

    last_resort = os.environ.get("ROBOTHOR_LAST_RESORT_MODEL", "").strip()
    if not last_resort:
        return
    from robothor.engine.model_registry import _MODEL_REGISTRY

    if last_resort not in _MODEL_REGISTRY:
        warnings.append(
            f"last-resort model {last_resort!r} (ROBOTHOR_LAST_RESORT_MODEL) is not in "
            "the model registry — every agent's chain ends in a model nothing can serve"
        )


def _check_model_block(warnings: list[str], where: str, model: Any) -> None:
    """Every model a manifest names must exist in the model registry.

    Nothing checked this before, and it let a fallback chain end in fiction:
    the fleet's last-resort tier named a model no server could serve for ~30
    hours. During a real outage the chain would have burned 2 x 600s Ollama
    timeouts against nothing and then raised — a dead tier makes outages
    SLOWER, and no log line says why. A typo'd name is just as silent:
    ``get_model_limits`` degrades to a generic 128K fallback with a warning
    nobody reads.

    ``${VAR}`` placeholders are skipped — unresolved env vars are a different
    problem, reported by the loader's own expansion.
    """
    if not isinstance(model, dict):
        return
    # Deferred: config_schema imports nothing heavy at module level, and the
    # registry pulls in litellm's catalog machinery.
    from robothor.engine.model_registry import _MODEL_REGISTRY

    names = [model.get("primary", "")]
    fallbacks = model.get("fallbacks", [])
    if isinstance(fallbacks, list):
        names.extend(fallbacks)
    for name in names:
        if not isinstance(name, str) or not name or "${" in name:
            continue
        if name not in _MODEL_REGISTRY:
            warnings.append(
                f"{where}: model {name!r} is not in the model registry — "
                "it will be skipped or mis-limited at dispatch (add it to "
                "robothor/engine/model_registry.py or fix the name)"
            )


def _key_home_map() -> dict[str, str]:
    """Which block each manifest key is actually read from.

    Derived from `config.py`, like the v2 key set, because every
    hand-maintained list in this project has eventually drifted from the
    code it describes.

    Only keys read from EXACTLY ONE block are included. `safety_cap` is read
    from both `v2:` and `schedule:` and is therefore not misplaceable — a
    map that claimed otherwise would emit a confident, wrong warning, which
    is worse than none.
    """
    import re
    from pathlib import Path as _Path

    try:
        src = (_Path(__file__).parent / "config.py").read_text(encoding="utf-8")
    except OSError:
        return {}
    homes: dict[str, set[str]] = {}
    for block in ("v2", "schedule", "model", "delivery"):
        pattern = rf"""{block}\.get\(\s*['"]([a-z0-9_]+)"""
        for key in re.findall(pattern, src):
            homes.setdefault(key, set()).add(block)
    return {key: next(iter(blocks)) for key, blocks in homes.items() if len(blocks) == 1}


#: A key here that turns up in the wrong block is reported, because a REAL
#: key in the WRONG block silently does nothing while looking deliberate.
#: `bench/wildclaw` carried `rate_limit_per_minute: 300` under `schedule:`
#: (read from `v2:`) and ran throttled at the 30/min default through every
#: measurement taken after the knob supposedly shipped — with a comment above
#: it explaining why the throttle needed raising.
#:
#: An unrecognised key is never reported: it may be a future field or an
#: instance extension, and warning about those would make this noise. Noisy
#: warnings get muted, and a muted warning is what this exists to prevent.
_KEY_HOME: dict[str, str] = _key_home_map()


def _check_misplaced_keys(warnings: list[str], data: dict[str, Any]) -> None:
    """Report keys that sit in a block other than the one they are read from."""
    for block in ("schedule", "v2", "model", "delivery"):
        section = data.get(block)
        if not isinstance(section, dict):
            continue
        for key in section:
            home = _KEY_HOME.get(key)
            if home and home != block:
                warnings.append(
                    f"{block}.{key} is ignored — {key!r} is read from the "
                    f"{home!r} block. Move it under {home}: or it silently "
                    "does nothing."
                )


def validate_manifest(data: dict[str, Any]) -> list[str]:
    """Validate a merged manifest dict. Returns list of warning strings (empty = valid)."""
    warnings: list[str] = []

    # Required fields
    if not data.get("id"):
        warnings.append("Missing required field: id")

    _check_misplaced_keys(warnings, data)

    # Model blocks — top-level, heartbeat, and worker all carry one, and the
    # 2026-08-23 incident's broken entry was in the HEARTBEAT block.
    _check_model_block(warnings, "model", data.get("model"))
    _check_last_resort_model(warnings)
    for section in ("heartbeat", "worker"):
        sub = data.get(section)
        if isinstance(sub, dict):
            _check_model_block(warnings, f"{section}.model", sub.get("model"))

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
