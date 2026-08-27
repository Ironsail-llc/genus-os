"""Discover, validate and load third-party extensions.

Three rules, and each exists because of how this platform has failed before:

* **Fail closed on a contract mismatch.** A plugin declares the version it
  was built against; anything else is refused and logged. An agent platform
  loading third-party code that expects a different tool-calling contract is
  a security problem, not a compatibility inconvenience.
* **Isolate every load.** One broken plugin must not stop the engine
  booting. Failures are recorded and returned, never raised.
* **All-or-nothing per plugin.** A plugin that half-loads is running in a
  state its author never tested.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

logger = logging.getLogger(__name__)

#: The extension contract. Bump on any breaking change to what a plugin
#: receives or must return; plugins declaring a different version are
#: refused rather than loaded and hoped for.
CONTRACT_VERSION = "1.0"

#: Entry-point group -> the payload key it contributes.
_GROUPS = {
    "genus.tools": "handlers",
    "genus.schemas": "schemas",
    "genus.guardrails": "policies",
    "genus.hooks": "hooks",
    # Model coverage. A hardcoded table cannot know every model an instance
    # runs: `openrouter/z-ai/glm-5.2` logged "add it to _MODEL_REGISTRY" 655
    # times in one benchmark run here. Plugins extend coverage; they never
    # overwrite a curated entry (see model_registry.get_model_limits).
    "genus.models": "models",
    # Background work. Everything the engine runs on a schedule was
    # registered from inside the package, so a third-party capability could
    # contribute a tool but nothing that runs on its own.
    "genus.jobs": "jobs",
    # Operator verbs. `robothor <verb>` was a fixed list of add_parser calls,
    # so an instance shipping its own operational command had to patch the
    # platform to expose it.
    "genus.commands": "commands",
    # Sandbox runtimes. Unlike every other group here, an installed backend
    # is INERT until the operator names it — see sandbox.active_sandbox_backend.
    "genus.sandboxes": "sandboxes",
}


@dataclass(frozen=True)
class PluginFailure:
    """One plugin that did not load, and why. Never raised — reported."""

    name: str
    group: str
    reason: str


@dataclass
class PluginSet:
    """Everything the installed plugins contributed, plus what refused."""

    tools: dict[str, Any] = field(default_factory=dict)
    schemas: dict[str, Any] = field(default_factory=dict)
    guardrails: dict[str, Any] = field(default_factory=dict)
    hooks: dict[str, Any] = field(default_factory=dict)
    models: dict[str, Any] = field(default_factory=dict)
    jobs: dict[str, Any] = field(default_factory=dict)
    commands: dict[str, Any] = field(default_factory=dict)
    sandboxes: dict[str, Any] = field(default_factory=dict)
    loaded: list[Any] = field(default_factory=list)
    failures: list[PluginFailure] = field(default_factory=list)

    def _target(self, group: str) -> dict[str, Any]:
        return {
            "genus.tools": self.tools,
            "genus.schemas": self.schemas,
            "genus.guardrails": self.guardrails,
            "genus.hooks": self.hooks,
            "genus.models": self.models,
            "genus.jobs": self.jobs,
            "genus.commands": self.commands,
            "genus.sandboxes": self.sandboxes,
        }[group]


#: Bumped by :func:`reload_plugins`. Every cache built from plugin
#: discovery records the value it was built at and rebuilds when it falls
#: behind, which is what lets a per-instance cache — the guardrail engine's,
#: the hook registry's — be invalidated without anyone tracking instances.
_generation = 0


def generation() -> int:
    """The current plugin generation. Caches store this beside their data."""
    return _generation


def reload_plugins() -> int:
    """Re-discover installed plugins without restarting the process.

    Genus cached discovery in four places and invalidated none of them, so
    installing a capability only took effect after a restart — which on this
    fleet cancels every in-flight run. Bumping the counter marks all four
    stale at once; each rebuilds lazily on its next read, so a reload costs
    nothing until something actually asks.

    ``importlib.invalidate_caches()`` is what makes a package installed since
    interpreter start visible to entry-point discovery.
    """
    global _generation
    importlib.invalidate_caches()
    _generation += 1
    logger.info("Plugin reload requested — generation now %d", _generation)
    return _generation


def _discover() -> list[Any]:
    found: list[Any] = []
    for group in _GROUPS:
        try:
            found.extend(metadata.entry_points(group=group))
        except Exception as e:  # a broken distribution must not stop boot
            logger.warning("Plugin discovery failed for %s: %s", group, e)
    return found


def load_plugins(
    entry_points: list[Any] | None = None,
    reserved_names: set[str] | None = None,
) -> PluginSet:
    """Load every installed plugin, refusing anything that does not fit.

    `entry_points` is injectable for testing; the default consults the real
    registry. `reserved_names` are names the host already owns — a plugin
    silently replacing `exec` or `write_file` would be a takeover, not an
    extension.
    """
    result = PluginSet()
    reserved = reserved_names or set()
    eps = _discover() if entry_points is None else entry_points

    for ep in eps:
        group = getattr(ep, "group", "")
        name = getattr(ep, "name", "<unnamed>")
        if group not in _GROUPS:
            continue

        try:
            payload = ep.load()
        except Exception as e:
            result.failures.append(
                PluginFailure(name, group, f"failed to import: {type(e).__name__}: {e}")
            )
            logger.warning("Plugin %r failed to import: %s", name, e)
            continue

        if not isinstance(payload, dict):
            result.failures.append(
                PluginFailure(name, group, f"payload is {type(payload).__name__}, expected a dict")
            )
            continue

        declared = payload.get("genus_contract_version")
        if declared != CONTRACT_VERSION:
            result.failures.append(
                PluginFailure(
                    name,
                    group,
                    f"contract version {declared!r} != {CONTRACT_VERSION!r} — refused",
                )
            )
            logger.warning(
                "Plugin %r declares contract %r, this engine speaks %r — refused",
                name,
                declared,
                CONTRACT_VERSION,
            )
            continue

        contributions = payload.get(_GROUPS[group])
        if not isinstance(contributions, dict) or not contributions:
            result.failures.append(PluginFailure(name, group, f"no {_GROUPS[group]!r} in payload"))
            continue

        target = result._target(group)
        clash = [k for k in contributions if k in reserved]
        if clash:
            result.failures.append(
                PluginFailure(name, group, f"reserved name(s) {sorted(clash)} — refused")
            )
            logger.warning("Plugin %r tried to shadow built-in(s) %s", name, sorted(clash))
            continue
        taken = [k for k in contributions if k in target]
        if taken:
            result.failures.append(
                PluginFailure(name, group, f"name(s) {sorted(taken)} already claimed — refused")
            )
            continue

        # All-or-nothing: validated above, applied here.
        target.update(contributions)
        result.loaded.append(ep)
        logger.info("Plugin %r loaded %d %s", name, len(contributions), _GROUPS[group])

    return result
