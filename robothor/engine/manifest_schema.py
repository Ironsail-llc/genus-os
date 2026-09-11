"""One validator for agent manifests, with the schema file as its only source.

Before this module there were two descriptions of what a manifest may contain
and they had never met. ``docs/agents/schema.yaml`` was read by a repo script;
``config_schema.validate_manifest`` was a hand-written list of checks that had
never read it; and ``README`` claimed the schema was enforced at startup, which
it was not. A key that was a typo of a real key was therefore accepted in
silence, did nothing, and looked deliberate — the same shape as the 2026-08-24
manifest outage.

Two halves fix that:

* **Structural rules are derived from the schema file at import time.** Adding a
  field to the schema teaches the validator about it. Nothing here is a second
  hand-maintained list — this project has shipped that defect enough times
  (``_KNOWN_V2_KEYS``, the guardrail list, the alert-name list) to stop.
* **Semantic rules move here verbatim** from ``config_schema``. Their messages
  are load-bearing: other tests grep them, and rewording one silently breaks a
  guard somewhere else.

Enforcement is a ladder, not a switch: ``ROBOTHOR_MANIFEST_SCHEMA_MODE`` is
``off`` | ``observe`` (default) | ``enforce``. ``observe`` logs and counts what
``enforce`` would have rejected, so the decision to promote rests on a count
taken from the instance's own manifests rather than on hope.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

#: The schema the engine reads. Ships inside the package (``packages =
#: ["robothor"]`` in pyproject) so a wheel install validates exactly what the
#: repo does. ``docs/agents/schema.yaml`` is a byte-equal documentation mirror,
#: pinned by ``tests/test_manifest_schema_single_source.py``.
SCHEMA_PATH = Path(__file__).parent / "schema" / "agent_manifest.yaml"

#: Every code this module can emit. A code is the stable half of an issue —
#: messages are for humans, codes are what a log filter, a dashboard and the
#: doctor match on, so they are enumerated rather than invented at the call
#: site.
ISSUE_CODES = frozenset(
    {
        # structural, derived from the schema file
        "missing_required",
        "wrong_type",
        "invalid_enum",
        "unknown_key",
        "schema_unreadable",
        # semantic, moved verbatim from config_schema
        "misplaced_key",
        "model_not_registered",
        "stall_budget_too_small",
        "out_of_range",
        "suspicious_cron",
        "unknown_delivery_mode",
        "unknown_session_target",
        "unknown_v2_key",
        "unknown_guardrail",
        "unknown_difficulty_class",
        "unknown_sandbox_mode",
        "lifecycle_hook_invalid",
    }
)

MODE_OFF = "off"
MODE_OBSERVE = "observe"
MODE_ENFORCE = "enforce"
_MODES = (MODE_OFF, MODE_OBSERVE, MODE_ENFORCE)


@dataclass(frozen=True)
class ManifestIssue:
    """One problem with one manifest.

    ``path`` is dotted and indexed the way the manifest is written
    (``v2.lifecycle_hooks[0].event``), so an operator can go straight to the
    line. ``code`` is stable; ``message`` is not.
    """

    path: str
    code: str
    message: str
    severity: str  # "error" | "warning"


class ManifestSchemaError(Exception):
    """Raised under ``enforce`` when a manifest has schema errors.

    Carries the issues rather than a rendered string: the caller decides how
    much to say, and ``_load_manifest_classified`` deliberately says less —
    a manifest failure ends up on an operator page, and platform code must not
    surface instance values (CLAUDE.md rules 1 and 2).
    """

    def __init__(self, agent_id: str, issues: list[ManifestIssue]) -> None:
        self.agent_id = agent_id
        self.issues = issues
        super().__init__(f"{agent_id}: {len(issues)} manifest schema error(s)")

    def summary(self) -> str:
        """``path (code)`` pairs only — never a value from the manifest."""
        return f"{len(self.issues)} schema issue(s): " + ", ".join(
            f"{i.path or '<root>'} ({i.code})" for i in self.issues
        )


# ── The schema file, read once ──────────────────────────────────────


@dataclass(frozen=True)
class _Field:
    """One node of the schema tree.

    ``children`` non-empty means an object whose keys are enumerated (so an
    unrecognised key there is reportable). ``children`` empty with
    ``type == "object"`` means free-form — ``goals`` and a ``when`` clause's
    ``overrides`` are manifest fragments, and guessing at their keys would
    produce confident, wrong warnings.
    """

    type: str | None = None
    enum: tuple[Any, ...] | None = None
    children: dict[str, _Field] | None = None
    items: dict[str, _Field] | None = None


def _parse_field(node: Any) -> _Field:
    """Turn one schema node into a ``_Field``.

    A node that carries a ``type:`` is a leaf spec; anything else is a nested
    block whose keys are its properties. That is the whole grammar of
    ``schema.yaml`` — it was written to be read by humans, and this keeps it
    that way rather than rewriting it as JSON Schema, which would have made
    the doc worse to serve the validator.
    """
    if not isinstance(node, dict):
        return _Field()
    if "type" in node:
        raw_enum = node.get("enum")
        props = node.get("properties")
        items = node.get("items")
        return _Field(
            type=str(node["type"]),
            enum=tuple(raw_enum) if isinstance(raw_enum, list) else None,
            children=(
                {k: _parse_field(v) for k, v in props.items()} if isinstance(props, dict) else None
            ),
            items=(
                {k: _parse_field(v) for k, v in items.items()} if isinstance(items, dict) else None
            ),
        )
    # A nested block. Its keys are field names unless the node is a bare spec
    # fragment with no recognisable children.
    children = {k: _parse_field(v) for k, v in node.items() if isinstance(k, str)}
    return _Field(type="object", children=children)


@dataclass(frozen=True)
class _Schema:
    required: frozenset[str]
    fields: dict[str, _Field]


def _load_schema(path: Path = SCHEMA_PATH) -> _Schema:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("agent manifest schema is not a mapping")
    fields: dict[str, _Field] = {}
    for section in ("required", "recommended", "optional"):
        block = raw.get(section) or {}
        if not isinstance(block, dict):
            continue
        for name, node in block.items():
            fields[str(name)] = _parse_field(node)
    required = frozenset(str(k) for k in (raw.get("required") or {}))
    return _Schema(required=required, fields=fields)


try:
    _SCHEMA: _Schema | None = _load_schema()
    _SCHEMA_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - a broken schema file is a build defect
    # Fail open, loudly. A validator must never be the reason an instance
    # cannot boot: that turns a typo-catcher into the outage it was written to
    # prevent.
    _SCHEMA = None
    _SCHEMA_ERROR = type(exc).__name__
    logger.error(
        "Agent manifest schema could not be loaded (%s) — structural checks are off", _SCHEMA_ERROR
    )


def required_keys() -> frozenset[str]:
    """Top-level keys the schema marks required."""
    return _SCHEMA.required if _SCHEMA else frozenset()


def known_top_level_keys() -> frozenset[str]:
    """Every top-level key the schema describes."""
    return frozenset(_SCHEMA.fields) if _SCHEMA else frozenset()


# ── Structural checking ─────────────────────────────────────────────

_LIST_RE = re.compile(r"^list\[(.+)\]$")

_SCALARS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
}


def _type_ok(value: Any, typename: str) -> bool:
    expected = _SCALARS.get(typename)
    if expected is None:
        return True
    # YAML has no separate boolean-as-integer problem, Python does: `True` is
    # an `int`, so `max_iterations: true` would sail through an isinstance
    # check and then be used as 1.
    if typename in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def _check_value(
    issues: list[ManifestIssue], path: str, value: Any, field: _Field, strict: bool
) -> None:
    if field.type is None:
        return
    list_match = _LIST_RE.match(field.type)
    if list_match:
        if not isinstance(value, list):
            _wrong_type(issues, path, value, "a list")
            return
        inner = list_match.group(1)
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            if field.items:
                _check_object(issues, item_path, item, field.items, strict)
            elif inner in _SCALARS and not _type_ok(item, inner):
                _wrong_type(issues, item_path, item, inner)
        return
    if not _type_ok(value, field.type):
        _wrong_type(issues, path, value, field.type)
        return
    if field.enum is not None and value not in field.enum:
        issues.append(
            ManifestIssue(
                path=path,
                code="invalid_enum",
                message=f"{path}={value!r} is not one of {sorted(map(str, field.enum))}",
                severity="error",
            )
        )
        return
    if field.children and isinstance(value, dict):
        _check_object(issues, path, value, field.children, strict)


def _check_object(
    issues: list[ManifestIssue],
    path: str,
    value: Any,
    children: dict[str, _Field],
    strict: bool,
) -> None:
    if not isinstance(value, dict):
        _wrong_type(issues, path, value, "a mapping")
        return
    for key, child_value in value.items():
        child_path = f"{path}.{key}" if path else str(key)
        field = children.get(str(key))
        if field is None:
            issues.append(
                ManifestIssue(
                    path=child_path,
                    code="unknown_key",
                    message=f"Unknown key: {child_path}",
                    severity="error" if strict else "warning",
                )
            )
            continue
        if child_value is None:
            # An empty block (`v2:` with nothing under it) parses as None.
            # That is "absent", not "wrong".
            continue
        _check_value(issues, child_path, child_value, field, strict)


def _wrong_type(issues: list[ManifestIssue], path: str, value: Any, expected: str) -> None:
    issues.append(
        ManifestIssue(
            path=path,
            code="wrong_type",
            message=f"{path} should be {expected}, got {type(value).__name__}",
            severity="error",
        )
    )


def _check_structure(issues: list[ManifestIssue], data: dict[str, Any], strict: bool) -> None:
    if _SCHEMA is None:
        issues.append(
            ManifestIssue(
                path="",
                code="schema_unreadable",
                message=f"Agent manifest schema could not be loaded ({_SCHEMA_ERROR})",
                severity="warning",
            )
        )
        return
    issues.extend(
        ManifestIssue(
            path=name,
            code="missing_required",
            message=f"Missing required field: {name}",
            severity="error",
        )
        for name in sorted(_SCHEMA.required)
        if data.get(name) in (None, "")
    )
    _check_object(issues, "", data, _SCHEMA.fields, strict)


# ── Semantic rules (moved verbatim from config_schema) ──────────────
#
# Every message below is copied character for character from the hand-written
# validator these replaced. Tests elsewhere in the suite grep them, and more
# importantly an operator has read them before: rewording a warning is a
# silent change to a signal somebody may already be filtering on.


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
        src = (Path(__file__).parent / "config.py").read_text(encoding="utf-8")
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


def _warn(issues: list[ManifestIssue], path: str, code: str, message: str) -> None:
    """Semantic findings are advisory, exactly as they were before.

    They describe things that are wrong but loadable — a model that is not in
    the registry, a key in the wrong block. Promoting any of them to an error
    would make `enforce` refuse manifests that run fine today, which is how a
    ladder gets turned off and left off.
    """
    issues.append(ManifestIssue(path=path, code=code, message=message, severity="warning"))


def _check_last_resort_model(issues: list[ManifestIssue]) -> None:
    """The model every agent's chain ends in is appended AFTER validation.

    ``_with_last_resort`` (config.py) adds ``ROBOTHOR_LAST_RESORT_MODEL`` to
    every chain inside ``manifest_to_agent_config``, which runs after
    ``validate_manifest``. So the one model the entire fleet's offline tier
    depends on was the one model nothing could check — a typo in robothor.env,
    or an ``ollama rm``, produced a fleet-wide chain ending in fiction with
    zero warnings on any agent. That is the exact failure ``_check_model_block``
    was written to prevent, one layer above where it could see it.
    """
    last_resort = os.environ.get("ROBOTHOR_LAST_RESORT_MODEL", "").strip()
    if not last_resort:
        return
    from robothor.engine.model_registry import _MODEL_REGISTRY

    if last_resort not in _MODEL_REGISTRY:
        _warn(
            issues,
            "model",
            "model_not_registered",
            f"last-resort model {last_resort!r} (ROBOTHOR_LAST_RESORT_MODEL) is not in "
            "the model registry — every agent's chain ends in a model nothing can serve",
        )


def _check_stall_budget_vs_llm_timeout(
    issues: list[ManifestIssue], where: str, model: Any, schedule: Any
) -> None:
    """A stall budget must exceed the per-call allowance for its own chain.

    Otherwise the watchdog is GUARANTEED to fire on a call the LLM layer still
    considers healthy — not occasionally, always. Six manifests carried 120s or
    180s budgets tuned against cloud models; when the fleet fell back to the
    local tier, where a single call is allowed 600s, the watchdog killed 33
    healthy runs in a day.

    Compared against the SCALED budget, because tier-aware budgets already
    multiply the manifest number by the chain's slowest model. A raw 300 is a
    scaled 900 on the local tier and is fine; a raw 120 is a scaled 360.

    Hand-auditing found six of eight. This exists so nobody has to read YAML by
    eye again.
    """
    if not isinstance(schedule, dict):
        return

    from robothor.engine.llm_client import LLM_REQUEST_TIMEOUT, LLM_REQUEST_TIMEOUT_OLLAMA
    from robothor.engine.model_registry import chain_tempo_factor

    chain: list[str] = []
    if isinstance(model, dict):
        primary = model.get("primary")
        if isinstance(primary, str) and primary:
            chain.append(primary)
        fallbacks = model.get("fallbacks")
        if isinstance(fallbacks, list):
            chain.extend([m for m in fallbacks if isinstance(m, str) and m])
    # Every agent's chain is terminated by the instance's last-resort model
    # (config._with_last_resort), so it belongs in the comparison even though
    # no manifest names it.
    last_resort = os.environ.get("ROBOTHOR_LAST_RESORT_MODEL", "").strip()
    if last_resort and last_resort not in chain:
        chain.append(last_resort)
    if not chain:
        return

    allowance = max(
        LLM_REQUEST_TIMEOUT_OLLAMA
        if m.startswith(("ollama_chat/", "ollama/"))
        else LLM_REQUEST_TIMEOUT
        for m in chain
    )
    factor = chain_tempo_factor(chain)

    for key in ("stall_timeout_seconds", "early_stall_timeout_seconds"):
        raw = schedule.get(key)
        if not isinstance(raw, int) or raw <= 0:
            continue  # 0 disables the watchdog; that is a choice, not a defect
        if raw * factor <= allowance:
            _warn(
                issues,
                f"{where}.{key}",
                "stall_budget_too_small",
                f"{where}.{key}={raw} scales to {int(raw * factor)}s, which is not more than the "
                f"{allowance}s this chain allows a single LLM call — the watchdog will fire on "
                f"healthy calls",
            )


def _check_model_block(issues: list[ManifestIssue], where: str, model: Any) -> None:
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
    # Deferred: this module imports nothing heavy at module level, and the
    # registry pulls in litellm's catalog machinery.
    from robothor.engine.model_registry import _MODEL_REGISTRY

    names = [model.get("primary", "")]
    fallbacks = model.get("fallbacks", [])
    if isinstance(fallbacks, list):
        names.extend(fallbacks)
    for name in names:
        if not isinstance(name, str) or not name or "${" in name:
            continue
        limits = _MODEL_REGISTRY.get(name)
        if limits is None:
            _warn(
                issues,
                where,
                "model_not_registered",
                f"{where}: model {name!r} is not in the model registry — "
                "it will be skipped or mis-limited at dispatch (add it to "
                "robothor/engine/model_registry.py or fix the name)",
            )
            continue
        # A deprecated id still VALIDATES — the manifest keeps working and the
        # run is still sized and priced correctly. It must not pass silently,
        # though, and a warning that only says "deprecated" leaves the operator
        # with nowhere to go, so it names the replacement.
        if getattr(limits, "status", "current") == "deprecated":
            _warn(
                issues,
                where,
                "model_deprecated",
                f"{where}: model {name!r} is deprecated — use {limits.replaced_by!r} instead",
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
    try:
        src = (Path(__file__).parent / "config.py").read_text(encoding="utf-8")
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
#: An unrecognised key is never reported HERE: it may be a future field or an
#: instance extension. Structural `unknown_key` covers those, and only bites
#: under `strict`.
_KEY_HOME: dict[str, str] = _key_home_map()


def _check_misplaced_keys(issues: list[ManifestIssue], data: dict[str, Any]) -> None:
    """Report keys that sit in a block other than the one they are read from."""
    for block in ("schedule", "v2", "model", "delivery"):
        section = data.get(block)
        if not isinstance(section, dict):
            continue
        for key in section:
            home = _KEY_HOME.get(key)
            if home and home != block:
                _warn(
                    issues,
                    f"{block}.{key}",
                    "misplaced_key",
                    f"{block}.{key} is ignored — {key!r} is read from the "
                    f"{home!r} block. Move it under {home}: or it silently "
                    "does nothing.",
                )


def _check_range(
    issues: list[ManifestIssue],
    block: str,
    data: dict[str, Any],
    key: str,
    min_val: int | float,
    max_val: int | float,
) -> None:
    """Check a numeric field is within range, if present."""
    if key not in data:
        return
    val = data[key]
    path = f"{block}.{key}" if block else key
    if not isinstance(val, (int, float)):
        _warn(issues, path, "wrong_type", f"{key} should be numeric, got {type(val).__name__}")
        return
    if val < min_val or val > max_val:
        _warn(
            issues,
            path,
            "out_of_range",
            f"{key}={val} is outside expected range [{min_val}, {max_val}]",
        )


def _check_semantics(issues: list[ManifestIssue], data: dict[str, Any]) -> None:
    _check_misplaced_keys(issues, data)

    # Model blocks — top-level, heartbeat, and worker all carry one, and the
    # 2026-08-23 incident's broken entry was in the HEARTBEAT block.
    _check_model_block(issues, "model", data.get("model"))
    _check_last_resort_model(issues)
    _check_stall_budget_vs_llm_timeout(issues, "schedule", data.get("model"), data.get("schedule"))
    for section in ("heartbeat", "worker"):
        sub = data.get(section)
        if isinstance(sub, dict):
            _check_model_block(issues, f"{section}.model", sub.get("model"))
            # main's failure was in its heartbeat block, which carries its own
            # budgets AND its own model block — the same place the 2026-08-23
            # manifest incident hid.
            _check_stall_budget_vs_llm_timeout(
                issues, section, sub.get("model") or data.get("model"), sub
            )

    # Schedule ranges
    schedule = data.get("schedule", {})
    if isinstance(schedule, dict):
        _check_range(issues, "schedule", schedule, "timeout_seconds", 0, 86400)
        # Lower bound is 0, not 1: 0 is the documented "no check-in interval"
        # sentinel (schema.yaml; the run loop guards with `_checkin_interval > 0`
        # and safety_cap is what actually bounds the run). main sets it, so a
        # floor of 1 logged a warning for every agent on every schedule tick.
        # Negative values are still a mistake and still warn.
        _check_range(issues, "schedule", schedule, "max_iterations", 0, 10000)
        _check_range(issues, "schedule", schedule, "safety_cap", 1, 10000)
        _check_range(issues, "schedule", schedule, "stall_timeout_seconds", 0, 86400)
        cron = schedule.get("cron", "")
        if cron and isinstance(cron, str) and not _CRON_RE.match(cron):
            _warn(
                issues, "schedule.cron", "suspicious_cron", f"Suspicious cron expression: {cron!r}"
            )

    # Delivery mode
    delivery = data.get("delivery", {})
    if isinstance(delivery, dict):
        mode = delivery.get("mode", "none")
        if mode not in _KNOWN_DELIVERY_MODES:
            _warn(
                issues,
                "delivery.mode",
                "unknown_delivery_mode",
                f"Unknown delivery mode: {mode!r} (expected one of {sorted(_KNOWN_DELIVERY_MODES)})",
            )

    # Session target
    if isinstance(schedule, dict):
        target = schedule.get("session_target", "isolated")
        if target not in _KNOWN_SESSION_TARGETS:
            _warn(
                issues,
                "schedule.session_target",
                "unknown_session_target",
                f"Unknown session_target: {target!r}",
            )

    # v2 block
    v2 = data.get("v2", {})
    if isinstance(v2, dict):
        # Unknown v2 keys (typo detection)
        for key in v2:
            if key not in _KNOWN_V2_KEYS:
                _warn(
                    issues,
                    f"v2.{key}",
                    "unknown_v2_key",
                    f"Unknown v2 key: {key!r} — possible typo?",
                )

        # Guardrail names
        guardrails = v2.get("guardrails", [])
        if isinstance(guardrails, list):
            for g in guardrails:
                if g not in _KNOWN_GUARDRAILS:
                    _warn(issues, "v2.guardrails", "unknown_guardrail", f"Unknown guardrail: {g!r}")

        # Difficulty class
        dc = v2.get("difficulty_class", "")
        if dc not in _KNOWN_DIFFICULTY_CLASSES:
            _warn(
                issues,
                "v2.difficulty_class",
                "unknown_difficulty_class",
                f"Unknown difficulty_class: {dc!r}",
            )

        # Sandbox
        sb = v2.get("sandbox", "local")
        if sb not in _KNOWN_SANDBOX_MODES:
            _warn(issues, "v2.sandbox", "unknown_sandbox_mode", f"Unknown sandbox mode: {sb!r}")

        # Numeric ranges
        _check_range(issues, "v2", v2, "max_nesting_depth", 0, 3)
        _check_range(issues, "v2", v2, "sub_agent_max_iterations", 1, 100)
        _check_range(issues, "v2", v2, "sub_agent_timeout_seconds", 1, 3600)
        _check_range(issues, "v2", v2, "safety_cap", 1, 10000)
        _check_range(issues, "v2", v2, "progress_report_interval", 1, 10000)
        _check_range(issues, "v2", v2, "human_approval_timeout", 10, 3600)

        max_cost = v2.get("max_cost_usd", 0)
        if isinstance(max_cost, (int, float)) and max_cost < 0:
            _warn(
                issues,
                "v2.max_cost_usd",
                "out_of_range",
                f"max_cost_usd cannot be negative: {max_cost}",
            )

        # Lifecycle hooks basic structure
        hooks = v2.get("lifecycle_hooks", [])
        if isinstance(hooks, list):
            for i, hook in enumerate(hooks):
                if not isinstance(hook, dict):
                    continue
                path = f"v2.lifecycle_hooks[{i}]"
                for needed in ("event", "handler_type", "handler"):
                    if needed not in hook:
                        _warn(
                            issues,
                            path,
                            "lifecycle_hook_invalid",
                            f"lifecycle_hooks[{i}] missing {needed!r}",
                        )
                ht = hook.get("handler_type", "")
                if ht and ht not in ("python", "command", "http", "agent"):
                    _warn(
                        issues,
                        path,
                        "lifecycle_hook_invalid",
                        f"lifecycle_hooks[{i}] unknown handler_type: {ht!r}",
                    )


# ── Public API ──────────────────────────────────────────────────────


def validate(data: dict[str, Any], *, strict: bool = False) -> list[ManifestIssue]:
    """Every problem this validator can see in one merged manifest.

    ``strict`` promotes `unknown_key` from warning to error. It is what the
    repo's own template test runs, and what a scaffold should run: a template
    that ships an unrecognised key is a bug in the template, whereas a live
    instance may legitimately carry a field a plugin reads.

    Never raises — a broken schema file degrades to "no structural checks"
    plus one `schema_unreadable` warning. Use :func:`raise_if_invalid` when a
    caller wants the errors to stop the load.
    """
    if not isinstance(data, dict):
        return [
            ManifestIssue(
                path="",
                code="wrong_type",
                message=f"manifest should be a mapping, got {type(data).__name__}",
                severity="error",
            )
        ]
    issues: list[ManifestIssue] = []
    _check_structure(issues, data, strict)
    _check_semantics(issues, data)
    return issues


def errors(issues: list[ManifestIssue]) -> list[ManifestIssue]:
    """The subset that blocks a load under ``enforce``."""
    return [i for i in issues if i.severity == "error"]


def legacy_warnings(issues: list[ManifestIssue]) -> list[str]:
    """The ``list[str]`` shape ``config_schema.validate_manifest`` promises.

    Non-error ``unknown_key`` findings are dropped. This list feeds a
    boot-time log line; an unrecognised key may be a future field or an
    instance extension, and warning about those on every load makes the log
    noisy. A noisy log gets muted, and a muted log is the failure this
    validator exists to prevent. ``strict=True`` is where unknown keys bite.
    """
    return [i.message for i in issues if not (i.code == "unknown_key" and i.severity != "error")]


def raise_if_invalid(data: dict[str, Any], *, agent_id: str = "", strict: bool = False) -> None:
    """Raise :class:`ManifestSchemaError` if the manifest has schema errors."""
    found = errors(validate(data, strict=strict))
    if found:
        raise ManifestSchemaError(agent_id or str(data.get("id") or "?"), found)


# ── Mode ladder ─────────────────────────────────────────────────────

#: Agents whose manifest ``enforce`` would have refused, keyed by agent id.
#:
#: A plain dict on purpose. The promotion decision needs "which agents, how
#: often" answered from a running instance, and the doctor reads this
#: directly; the Prometheus counter below is the same number for dashboards.
#: Exported mutable so a test can assert on it without scraping /metrics.
manifest_schema_would_reject: dict[str, int] = {}


def record_would_reject(agent_id: str) -> None:
    """Count one manifest that ``enforce`` would have refused. Never raises."""
    manifest_schema_would_reject[agent_id] = manifest_schema_would_reject.get(agent_id, 0) + 1
    try:
        from robothor.engine.metrics import MANIFEST_SCHEMA_WOULD_REJECT

        MANIFEST_SCHEMA_WOULD_REJECT.labels(agent_id=agent_id).inc()
    except Exception:  # pragma: no cover - metrics are never load-bearing
        logger.debug("Could not increment manifest schema counter", exc_info=True)


def reset_would_reject() -> None:
    """Clear the counter. For tests and for the doctor's per-run snapshot."""
    manifest_schema_would_reject.clear()


def schema_mode() -> str:
    """``off`` | ``observe`` | ``enforce``, from the environment.

    Read with ``os.environ.get`` rather than a settings object: this is called
    from the manifest loader, which runs before anything else is configured,
    and an unparsable value must not be able to stop a boot. Anything
    unrecognised degrades to ``observe`` — the mode that reports without
    refusing.
    """
    raw = os.environ.get("ROBOTHOR_MANIFEST_SCHEMA_MODE", MODE_OBSERVE).strip().lower()
    if raw not in _MODES:
        if raw:
            logger.warning(
                "Unknown ROBOTHOR_MANIFEST_SCHEMA_MODE %r — falling back to %r", raw, MODE_OBSERVE
            )
        return MODE_OBSERVE
    return raw
