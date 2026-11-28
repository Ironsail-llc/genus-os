"""What a write tool acts on, and how its uncertain outcome is read back.

The durable effect fence (``effects.begin``) refuses a write while an earlier
write's outcome is unknown. Before, a goal-less uncertain write refused every
later call of the same tool by that principal, to any target, for a day. The
fence now compares the TARGET -- what the write acts on (a thread, a recipient,
a record) -- so a different action goes ahead while a repeat of the same one,
however it is reworded, still waits.

The fence narrows only where the target is KNOWN: the argument names a plugin
declares for its own tool (``EffectSpec.target``). Everywhere else -- every core
tool, and a plugin tool that declares no target -- every later call of the tool
still waits, as before. A target guessed from the arguments is not safe to
narrow on: a retried send that adds ``cc: ""`` or an optional flag would look
like another target and go out twice. ``fence="tool"`` keeps the whole-tool
scope for a tool that declares a target only for its read-back.

A plugin cannot declare any of this for a built-in tool: narrowing a core tool's
fence, or settling its writes, is not a plugin's to do.

A plugin may also register a read-back verifier for its write tool
(``EffectSpec.verify``). It is called with the effect record -- its ``id`` (pass
it to the provider as the idempotency or client message key when you send) and
its recorded ``target`` -- and returns an ``effects.Verification``. A settled
``applied`` or ``not_applied`` clears the fence; ``unknown`` leaves it. Plugins
declare both in their ``genus.tools`` payload under ``effects`` (docs/PLUGINS.md).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from robothor.engine.runtime import effects

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Argument names that carry a write's wording rather than what it acts on. A
#: reworded retry changes these and nothing else, so they are not the target.
PROSE_KEYS: frozenset[str] = frozenset(
    {
        "body",
        "text",
        "message",
        "content",
        "subject",
        "title",
        "note",
        "notes",
        "html",
        "markdown_body",
        "description",
        "summary",
        "comment",
        "caption",
        "reply",
        "finalReport",
    }
)

#: The longest a target value is kept in the summary an operator and a refusal see.
MAX_TARGET_VALUE = 120

FENCES = frozenset({"target", "tool"})


@dataclass(frozen=True)
class EffectSpec:
    """A plugin's declaration for one of its own write tools."""

    #: The argument names that say what the write acts on. None: derive them.
    target: tuple[str, ...] | None = None
    #: ``"target"`` (default): fence a repeat on the same target only.
    #: ``"tool"``: fence every later call of the tool, for a tool whose calls
    #: cannot be told apart by their arguments.
    fence: str = "target"
    #: Read-back: ``verify(record) -> effects.Verification``. Synchronous; it
    #: runs off the event loop. Must answer ``unknown`` when it cannot tell.
    verify: Callable[[dict[str, Any]], effects.Verification] | None = None


#: ``(plugin generation, specs)``; rebuilt when plugins reload.
_cache: tuple[int, dict[str, EffectSpec]] | None = None


def plugin_specs() -> dict[str, EffectSpec]:
    """Every installed plugin's effect declarations. Never raises."""
    global _cache
    try:
        from robothor.plugins import load_plugins
        from robothor.plugins.loader import generation

        current = generation()
        if _cache is not None and _cache[0] == current:
            return _cache[1]
        from robothor.engine.tools.dispatch import builtin_handlers

        # The tool registry refuses a plugin that shadows a built-in; this
        # loader call does not reserve names, so drop such declarations here,
        # as effect_dispatch does for read_only.
        core = set(builtin_handlers()) | set(effects.READBACK_TOOLS)
        loaded = load_plugins(reserved_names=set()).effects
        specs = {name: value for name, value in loaded.items() if name not in core}
        _cache = (current, specs)
        return specs
    except Exception as exc:  # noqa: BLE001 - one broken package is not an outage
        logger.debug("plugin effect declarations unavailable: %s", exc)
        return {}


def spec(tool_name: str) -> EffectSpec | None:
    return plugin_specs().get(tool_name)


#: Built-in writes whose target is derived by core, not declared by a plugin.
#: ``org_tool_write`` acts on one tool in one child company with one set of
#: arguments; the arguments are kept as a digest only (they may name anyone).
_CORE_TARGET_KEYS: dict[str, tuple[str, ...]] = {
    "org_tool_write": ("tenant", "tool", "args_digest"),
}


def _core_target(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from robothor.tenancy.remote_contract import args_digest

    inner = arguments.get("args")
    return {
        "tenant": arguments.get("tenant"),
        "tool": arguments.get("tool"),
        "args_digest": args_digest(inner if isinstance(inner, dict) else {}),
    }


def declared_target(tool_name: str) -> tuple[str, ...] | None:
    if tool_name in _CORE_TARGET_KEYS:
        return _CORE_TARGET_KEYS[tool_name]
    declared = spec(tool_name)
    return declared.target if declared is not None and declared.target else None


def target_of(tool_name: str, arguments: Any) -> dict[str, Any]:
    """What this write acts on: its declared target arguments, exactly as sent.

    A tool with no declared target is described (for an operator and a
    refusal) by its arguments that are not wording; that description never
    narrows the fence.
    """
    if not isinstance(arguments, dict):
        return {}
    if tool_name in _CORE_TARGET_KEYS:
        return _core_target(tool_name, arguments)
    keys = declared_target(tool_name)
    if keys:
        return {key: arguments.get(key) for key in keys}
    return {
        key: value
        for key, value in arguments.items()
        if key not in PROSE_KEYS and not str(key).startswith("_")
    }


def target_key(tool_name: str, target: dict[str, Any]) -> str | None:
    """The hash the fence compares; None (fence the whole tool) without a declared target."""
    if not declared_target(tool_name):
        return None
    payload = json.dumps([tool_name, target], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def stored(tool_name: str, target: dict[str, Any]) -> dict[str, Any]:
    """The target as an effects row keeps it, redacted.

    A declared target is kept exactly as sent (the verifier looks it up with
    it). A described one -- every non-wording argument of a tool that declares
    none -- is cut short, since it may carry anything.
    """
    from robothor.secrets.redaction import redact_tool_result

    kept = target if declared_target(tool_name) else {k: short(v) for k, v in target.items()}
    redacted = redact_tool_result(kept)
    return redacted if isinstance(redacted, dict) else {}


def short(value: Any) -> Any:
    """One target value as a person reads it: at most :data:`MAX_TARGET_VALUE` long."""
    if value is None or isinstance(value, bool | int | float):
        return value
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= MAX_TARGET_VALUE else text[: MAX_TARGET_VALUE - 1] + "…"


def fences_whole_tool(tool_name: str) -> bool:
    """Whether every later call of the tool waits on an uncertain one: unless it
    declares a target and has not asked for the whole-tool scope."""
    if tool_name in _CORE_TARGET_KEYS:
        return False
    declared = spec(tool_name)
    return declared is None or not declared.target or declared.fence == "tool"


def verifier(tool_name: str) -> Callable[[dict[str, Any]], effects.Verification] | None:
    declared = spec(tool_name)
    return declared.verify if declared is not None else None


def has_verifier(tool_name: str) -> bool:
    """Whether a read-back can settle this tool's uncertain effects."""
    return tool_name in effects.READBACK_TOOLS or verifier(tool_name) is not None


def recover(scope: effects.EffectScope, effect_id: Any) -> dict[str, Any] | None:
    """Read an uncertain effect back. The original result when it was applied, else None.

    Never raises and never retries the write: a verifier that fails or cannot
    tell leaves the effect uncertain and the fence in place.
    """
    try:
        record = effects.read(scope, effect_id)
        if not record:
            return None
        tool_name = record["tool_name"]
        if tool_name in effects.READBACK_TOOLS:
            from robothor.engine.runtime import note_recovery, task_recovery

            core = {"create_task": task_recovery.recover, "create_note": note_recovery.recover}
            return core[tool_name](scope, effect_id)
        verify = verifier(tool_name)
        if verify is None:
            return None
        if record["state"] == "uncertain":
            effects.resolve(scope, effect_id, verify)
            record = effects.read(scope, effect_id)
        if record and record["state"] == "confirmed":
            return {
                **(record["resolution"] or {}).get("result", {}),
                "effect_id": str(effect_id),
                "recovered": True,
            }
    except Exception:
        logger.warning("Read-back of an uncertain effect deferred", exc_info=True)
    return None


def sweep(tenant_id: str) -> int:
    """Run every plugin verifier over its tool's uncertain effects. Bounded; never raises."""
    tools = sorted(name for name, declared in plugin_specs().items() if declared.verify)
    if not tools:
        return 0
    settled = 0
    try:
        from robothor.db.connection import tenant_scope

        with tenant_scope(tenant_id), effects.get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id,principal_id FROM agent_runtime_effects
                   WHERE tenant_id=%s AND state='uncertain' AND tool_name = ANY(%s)
                   ORDER BY updated_at,id LIMIT 100""",
                (tenant_id, tools),
            )
            rows = cur.fetchall()
        for identifier, principal_id in rows:
            scope = SimpleNamespace(tenant_id=tenant_id, principal_id=principal_id)
            recover(scope, identifier)
            record = effects.read(scope, identifier)
            if record and record["state"] != "uncertain":
                settled += 1
                continue
            # Rotate an unsettled read-back so it cannot starve later records.
            with tenant_scope(tenant_id), effects.get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_runtime_effects SET updated_at=now() "
                    "WHERE id=%s AND tenant_id=%s AND state='uncertain'",
                    (identifier, tenant_id),
                )
    except Exception:
        logger.warning("Plugin effect read-back sweep deferred", exc_info=True)
    return settled
