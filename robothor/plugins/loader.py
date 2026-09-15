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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

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
    # Arbitrary named services. The other groups name a KIND the platform
    # knows about; this one does not, which is the point. DeepSeek Harness
    # exposes 143 distinct `ctx.*` surfaces because service registration is
    # its primitive rather than a list, and no number of named groups here
    # catches that by growing. Breadth becomes a shape, not a count — while
    # the contract version and reserved names still apply, which their
    # architecture doc addresses for none of theirs.
    "genus.services": "services",
    # Operator verbs. `robothor <verb>` was a fixed list of add_parser calls,
    # so an instance shipping its own operational command had to patch the
    # platform to expose it.
    "genus.commands": "commands",
    # Sandbox runtimes. Unlike every other group here, an installed backend
    # is INERT until the operator names it — see sandbox.active_sandbox_backend.
    "genus.sandboxes": "sandboxes",
    # Outbound/inbound surfaces. Genus reached people over exactly one, and the
    # code said so: delivery's announce branch called _deliver_telegram. Also
    # INERT until named, for the sandbox's reason — a package that became the
    # delivery surface by merely being installed could intercept every briefing
    # while nothing looked different. See channels.registry.get_channel.
    "genus.channels": "channels",
    # Additional memory sources. Hermes ships eight providers and leads three
    # of four published models; its design rule is the one worth copying —
    # providers run ALONGSIDE built-in memory and never replace it.
    "genus.memory": "providers",
    # Diagnostics. `genus doctor` asks a fixed set of questions about the host,
    # and a plugin that adds a capability can also add the question that tells
    # an operator whether it is configured -- which core cannot know. The
    # doctor's registry adds two rules on top of this group: an id must carry
    # the contributing distribution's name, and may not shadow a built-in.
    "genus.doctor": "checks",
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
    services: dict[str, Any] = field(default_factory=dict)
    commands: dict[str, Any] = field(default_factory=dict)
    sandboxes: dict[str, Any] = field(default_factory=dict)
    #: Channel implementations, read by ``engine.channels.registry``. Named for
    #: the GROUP like ``memory`` and ``doctor``, which is what
    #: test_plugin_groups_are_consumed.py enforces.
    channels: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    #: Doctor checks. Named for the GROUP, not for the payload key -- the
    #: same convention `memory` follows, and the one
    #: test_plugin_groups_are_consumed.py enforces so that a declared group
    #: always names a field something in production actually reads.
    doctor: dict[str, Any] = field(default_factory=dict)
    #: Tool names the providing plugin declared read-only. Absent means
    #: WRITE, which is the safe default and today's behaviour: a plugin
    #: that says nothing must never be assumed harmless.
    read_only: set[str] = field(default_factory=set)
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
            "genus.services": self.services,
            "genus.commands": self.commands,
            "genus.sandboxes": self.sandboxes,
            "genus.channels": self.channels,
            "genus.memory": self.memory,
            "genus.doctor": self.doctor,
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


#: Where the names core already owns come from, per group. Each value is a
#: zero-argument callable returning an iterable of names, and every one of them
#: is the SAME object the enforcing caller reads — ``dispatch`` really does pass
#: ``builtin_handlers()``, ``guardrails`` really does pass ``_KNOWN_POLICIES``.
#: That is the point: a second, hand-written list here is the defect
#: `hardcoded-names-drift` documents, and `test_plugin_reserved_names.py`
#: asserts each entry still equals what its caller passes.
#:
#: Groups absent from this table own no built-in names — ``genus.jobs``,
#: ``genus.sandboxes`` and ``genus.memory`` are extension points with nothing
#: in core to shadow, and all three of their production callers pass ``set()``.
#: A registry that starts owning names in one of them must add itself here;
#: ``test_plugin_reserved_names.py::TestNoDrift`` asserts one drift guard per
#: entry, so a new entry without one fails.
_BUILTIN_SOURCES: dict[str, str] = {
    "genus.tools": "robothor.engine.tools.dispatch:builtin_handlers",
    # NOT get_engine_schemas: `ToolRegistry` seeds `_schemas` from the MCP tool
    # definitions as well, and reserves all of them. Pointing at half of that
    # left 53 names (every CRM verb) reserved in production and derivable by
    # nobody.
    "genus.schemas": "robothor.engine.tools.registry:builtin_schema_names",
    "genus.guardrails": "robothor.engine.guardrails:_KNOWN_POLICIES",
    "genus.models": "robothor.engine.model_registry:_MODEL_REGISTRY",
    "genus.services": "robothor.engine.services:_RESERVED",
    "genus.channels": "robothor.engine.channels.registry:BUILTIN_CHANNELS",
    "genus.commands": "robothor.cli:builtin_command_names",
    "genus.doctor": "robothor.doctor.registry:builtin_ids",
    "genus.hooks": "robothor.engine.hook_registry:builtin_hook_names",
}

#: Groups whose built-in names are NOT fixed for the life of the process, and
#: so must never be cached.
#:
#: ``genus.hooks`` alone: every other source is a module constant or a table
#: built at import, but lifecycle handlers are registered DURING daemon boot
#: (``daemon.py`` registers three). A cached empty set taken before that — by a
#: doctor check, a listing, anything that loads plugins early — would freeze in
#: exactly the gap this table exists to close, and it would do so invisibly.
_UNCACHEABLE = frozenset({"genus.hooks"})

#: Resolved built-in names, per group. Built-ins do not change while the
#: process runs — a plugin reload cannot add one — so this is a process cache
#: rather than a generation-keyed one, and it keeps a listing from rebuilding
#: the engine schema table on every request. See ``_UNCACHEABLE``.
_builtin_cache: dict[str, set[str]] = {}

#: Guards against a built-in source that itself reaches back into plugin
#: discovery. None does today, and the cost of the guard is one boolean against
#: a recursion that would present as a hung engine rather than as an error.
_resolving: set[str] = set()


def reset_builtin_names() -> None:
    """Drop the built-in-name cache. For tests, and for nothing else."""
    _builtin_cache.clear()


def builtin_names(group: str) -> set[str]:
    """Names the host already owns in ``group`` — what a plugin may not claim.

    Derived from the live registry, never listed here. A plugin silently
    replacing ``exec`` or ``web_fetch`` is a takeover rather than an extension,
    and every enforcing caller already refuses it; this exists so the OPERATOR
    surfaces (``genus plugin list``, ``GET /api/admin/plugins``, the reload
    response, the ``plugins.load`` doctor check) refuse it too. They passed an
    empty set, so a distribution the engine refused was reported as loaded and
    a `required` check went green over it.

    Never raises. A registry that will not import contributes no names, which
    is the same posture the loader takes to everything else: a broken piece of
    the platform must not stop plugin discovery from answering.
    """
    cached = _builtin_cache.get(group)
    if cached is not None:
        return set(cached)
    target = _BUILTIN_SOURCES.get(group)
    if target is None or group in _resolving:
        return set()

    module_path, _, attribute = target.partition(":")
    _resolving.add(group)
    try:
        module = importlib.import_module(module_path)
        source = getattr(module, attribute)
        names = {str(name) for name in (source() if callable(source) else source)}
    except Exception as exc:  # noqa: BLE001 - a broken registry is not a plugin fault
        logger.warning("Built-in names for %s unavailable (%s)", group, type(exc).__name__)
        return set()
    finally:
        _resolving.discard(group)

    if group not in _UNCACHEABLE:
        _builtin_cache[group] = names
    return set(names)


def _platform_installed(dist: Any, lock: Any) -> bool:
    """Whether ``genus plugin install`` put this distribution here.

    Reads the lock row's ``source``, which only that command writes. Never
    raises: a lockfile this cannot read constrains nothing, which is the same
    posture every other reader here takes.
    """
    try:
        from robothor.plugins.lockfile import dist_name

        name = dist_name(dist)
        row = lock.rows.get(name) if name else None
    except Exception:  # noqa: BLE001 - governance must never block boot
        return False
    return bool(row is not None and row.source is not None)


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
    lockfile_path: Path | None = None,
) -> PluginSet:
    """Load every installed plugin, refusing anything that does not fit.

    `entry_points` is injectable for testing; the default consults the real
    registry. `reserved_names` are names the host already owns — a plugin
    silently replacing `exec` or `write_file` would be a takeover, not an
    extension.

    **Omitting `reserved_names` means the built-in set for each group**, from
    :func:`builtin_names`, not an empty one. An enforcing caller still passes
    its own registry's names; the default exists so that the OPERATOR surfaces
    — which have no registry of their own — measure what production measures.
    They passed `set()`, and a distribution the engine refused for shadowing
    `web_fetch` was reported as loaded by all three of them.

    `lockfile_path` is likewise injectable, and it is what keeps a test off the
    operator's real `plugins.lock`: the default resolves through the config-dir
    setting to a file that exists on any box where `genus plugin sync` has run.
    """
    result = PluginSet()
    per_group_reserved = reserved_names is None
    reserved = reserved_names or set()
    eps = _discover() if entry_points is None else entry_points

    # The operator's record, read ONCE per load rather than per entry point:
    # a distribution publishing into five groups must not cost five reads of
    # the same file on the boot path. Read here rather than cached at module
    # scope so that `genus plugin disable X` followed by a SIGHUP takes effect
    # -- reload_plugins() invalidates every cache built on top of this, and
    # each of them calls back in here, so a fresh read is the reload.
    from robothor.plugins import lockfile as _lockfile

    try:
        lock = _lockfile.read_lockfile(lockfile_path)
    except Exception as exc:  # noqa: BLE001 - governance must never block boot
        logger.warning("Plugin lockfile could not be read (%s); ignoring it", type(exc).__name__)
        lock = _lockfile.Lockfile()

    # One manifest digest per DISTRIBUTION, not per entry point. `genus-hostinfo`
    # publishes into three groups, so the un-memoized version read and hashed
    # its manifest three times per load on top of the three the parse costs.
    # Keyed on the distribution NAME: the metadata layer hands out a fresh
    # `Distribution` object per group query, so an id()-keyed memo never hit.
    digests: dict[str, str] = {}

    for ep in eps:
        group = getattr(ep, "group", "")
        name = getattr(ep, "name", "<unnamed>")
        if group not in _GROUPS:
            continue

        # THE LOCKFILE IS CONSULTED BEFORE `ep.load()`, for the reason the
        # manifest gate below gives: after the import, "refused" means the code
        # has already run. A distribution with no row is unconstrained -- the
        # lockfile governs what it has been told about, and `genus plugin sync`
        # is what tells it.
        refusal = _lockfile.refusal_for(getattr(ep, "dist", None), lock, digests=digests)
        if refusal is not None:
            result.failures.append(PluginFailure(name, group, refusal))
            logger.info("Plugin %r not loaded: %s", name, refusal)
            continue

        # GOVERNANCE BEFORE EXECUTION. `ep.load()` below imports the
        # distribution's module into this process; every other check in this
        # function happens after that, so without this gate a "refused" plugin
        # has already run arbitrary code inside the daemon. An undeclared
        # distribution is refused here, before it executes.
        from robothor.plugins.manifest import MANIFEST_NAME, manifest_mode, read_manifest

        _mode = manifest_mode()
        manifest = read_manifest(getattr(ep, "dist", None))
        if manifest is None and _mode != "off":
            if _mode == "enforce":
                result.failures.append(
                    PluginFailure(
                        name,
                        group,
                        f"no {MANIFEST_NAME} in the distribution — refused before import",
                    )
                )
                logger.warning("Plugin %r ships no %s; refusing before import", name, MANIFEST_NAME)
                continue
            logger.warning(
                "Plugin %r ships no %s — importing anyway (mode=observe). The "
                "pre-import guarantee applies only in enforce.",
                name,
                MANIFEST_NAME,
            )
        _declared = manifest.declares(_GROUPS[group]) if manifest else None

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

        # Hold the payload to what the distribution declared. This half is
        # necessarily post-import — a module's exports cannot be read without
        # executing it — but the manifest is what makes them reviewable BEFORE
        # install, and undeclared names are never registered.
        # ENFORCE unconditionally for a distribution THIS PLATFORM installed.
        # `manifest_mode` defaults to `observe` because requiring a manifest is
        # a breaking change for everything published before manifests existed —
        # but that grandfathering has no claim on a plugin that arrived through
        # `genus plugin install`, which could not have been installed at all
        # without a manifest the index had pinned. Leaving those on the shipped
        # default meant nothing compared declared names to actual surface at ANY
        # stage, while the scan's group-level trade was being defended by
        # pointing at this very check.
        _enforced = _mode == "enforce" or _platform_installed(getattr(ep, "dist", None), lock)
        undeclared = (
            sorted(k for k in contributions if k not in _declared)
            if (_declared is not None and _enforced)
            else []
        )
        if undeclared:
            result.failures.append(
                PluginFailure(name, group, f"undeclared in {MANIFEST_NAME}: {undeclared} — refused")
            )
            logger.warning("Plugin %r offered undeclared %s %s", name, group, undeclared)
            continue

        target = result._target(group)
        # The group's own built-in names when the caller named none. A caller
        # that DID name a set is not second-guessed: each registry owns a
        # different one, and the enforcing path must keep passing its own.
        group_reserved = builtin_names(group) if per_group_reserved else reserved
        clash = [k for k in contributions if k in group_reserved]
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

        # A plugin may classify its OWN tools as read-only. Safety
        # classification used to be core's hardcoded table, so extracting an
        # integration to a plugin left a fact about that instance behind in
        # core -- the fork this seam exists to prevent.
        declared_ro: set[str] = set()
        if group == "genus.tools" and "read_only" in payload:
            raw = payload.get("read_only")
            if not isinstance(raw, list | tuple | set) or not all(isinstance(x, str) for x in raw):
                result.failures.append(
                    PluginFailure(name, group, "read_only must be a list of tool names — refused")
                )
                continue
            foreign = sorted(set(raw) - set(contributions))
            if foreign:
                # Reclassifying a tool it does not provide is privilege
                # escalation, not extension.
                result.failures.append(
                    PluginFailure(
                        name,
                        group,
                        f"read_only names {foreign} are not provided by this plugin — refused",
                    )
                )
                logger.warning(
                    "Plugin %r tried to classify tool(s) it does not own: %s", name, foreign
                )
                continue
            declared_ro = set(raw)

        # All-or-nothing: validated above, applied here.
        target.update(contributions)
        result.read_only |= declared_ro
        result.loaded.append(ep)
        logger.info("Plugin %r loaded %d %s", name, len(contributions), _GROUPS[group])

    return result
