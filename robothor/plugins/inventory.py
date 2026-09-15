"""One answer to "what is installed, and what is the engine doing with it".

Three surfaces ask that question — ``genus plugin list``/``info``, the doctor's
``plugins.*`` checks, and ``GET /api/admin/plugins`` — and the platform has
been bitten three separate times by the same defect in the same shape: a
hand-maintained second list beside the thing it describes, drifting from it.
So the three read this, and none of them walks the entry-point registry itself.

The unit is the DISTRIBUTION, not the entry point. A distribution is what an
operator installs, what the lockfile records, and what ``enable``/``disable``
flip; one distribution routinely publishes into several groups (the shipped
``genus-hostinfo`` publishes into three), and reporting three rows for it would
make "is this plugin on?" a question with three answers.

The load state is measured by actually loading, per distribution, through the
real loader — which is what makes ``disabled`` here mean the same thing it
means in the daemon, gate and all. The honest limit: a per-distribution load
passes no ``reserved_names``, so a plugin refused in production solely for
shadowing a built-in of one particular registry reports as ``loaded`` here. The
registries each own a different reserved set, and inventing a union of them
would be a fourth list to drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["KINDS", "PluginStatus", "inventory", "status_for"]

#: The :class:`~robothor.plugins.loader.PluginSet` fields a plugin can fill,
#: in the order a report lists them. Named for the GROUP, which is the
#: convention ``test_plugin_groups_are_consumed.py`` enforces.
KINDS = (
    "tools",
    "schemas",
    "guardrails",
    "hooks",
    "models",
    "jobs",
    "services",
    "commands",
    "sandboxes",
    "channels",
    "memory",
    "doctor",
)


@dataclass(frozen=True)
class PluginStatus:
    """One installed distribution, as the engine currently sees it."""

    name: str
    version: str = ""
    #: Entry-point groups this distribution publishes into.
    groups: tuple[str, ...] = ()
    #: ``loaded`` | ``failed`` | ``disabled``. ``disabled`` is a decision, not
    #: a fault, and is reported apart from ``failed`` for that reason alone.
    state: str = "loaded"
    #: What the lockfile records. ``enabled`` is True for a distribution with
    #: no row: an unrecorded plugin is unconstrained, not off.
    recorded: bool = False
    enabled: bool = True
    verdict: str = ""
    #: True when the manifest on disk no longer hashes to the recorded value.
    drifted: bool = False
    failure_reason: str = ""
    #: kind -> how many names this distribution contributed to it. Only
    #: non-empty kinds appear, so a plugin's shape is legible at a glance.
    contributions: dict[str, int] = field(default_factory=dict)
    #: ``{"contract_version": int|None, "declared": {kind: [names]}}``, or None
    #: when the distribution ships no manifest.
    manifest: dict[str, Any] | None = None

    def as_json(self) -> dict[str, Any]:
        """The wire shape the admin API and the Helm read.

        No path of any kind: a distribution name and a version are platform
        facts, and where an instance keeps its files is not.
        """
        return {
            "name": self.name,
            "version": self.version,
            "enabled": self.enabled,
            "recorded": self.recorded,
            "verdict": self.verdict,
            "state": self.state,
            "drifted": self.drifted,
            "groups": list(self.groups),
            "contributions": dict(self.contributions),
            "failure_reason": self.failure_reason or None,
            "manifest": self.manifest,
        }


def _manifest_json(dist: Any) -> dict[str, Any] | None:
    from robothor.plugins.manifest import read_manifest

    manifest = read_manifest(dist)
    if manifest is None:
        return None
    return {
        "contract_version": manifest.contract_version,
        "declared": {kind: sorted(names) for kind, names in sorted(manifest.declared.items())},
    }


def status_for(name: str, dist: Any, eps: list[Any]) -> PluginStatus:
    """Measure one distribution by loading exactly its entry points."""
    from robothor.plugins import lockfile
    from robothor.plugins.loader import load_plugins

    lock = lockfile.read_lockfile()
    row = lock.row(name)
    loaded = load_plugins(entry_points=eps, reserved_names=set())

    contributions = {
        kind: len(getattr(loaded, kind, {}) or {}) for kind in KINDS if getattr(loaded, kind, None)
    }
    reason = loaded.failures[0].reason if loaded.failures else ""
    if reason == lockfile.DISABLED_REASON:
        state = "disabled"
    elif loaded.failures:
        state = "failed"
    else:
        state = "loaded"

    return PluginStatus(
        name=name,
        version=str(getattr(dist, "version", "") or ""),
        groups=tuple(sorted({getattr(ep, "group", "") for ep in eps} - {""})),
        state=state,
        recorded=row is not None,
        enabled=row.enabled if row is not None else True,
        verdict=row.verdict if row is not None else "",
        drifted=bool(row is not None and row.manifest_sha256 != lockfile.manifest_digest(dist)),
        failure_reason=reason,
        contributions=contributions,
        manifest=_manifest_json(dist),
    )


def inventory() -> tuple[PluginStatus, ...]:
    """Every installed plugin distribution, by name.

    A distribution the metadata layer cannot name is skipped rather than
    reported under a placeholder: ``enable``/``disable`` address a row by name,
    and a row keyed on the empty string is one no operator can act on.
    """
    from robothor.plugins import loader
    from robothor.plugins.lockfile import dist_name

    grouped: dict[str, list[Any]] = {}
    dists: dict[str, Any] = {}
    for ep in loader._discover():
        if getattr(ep, "group", "") not in loader._GROUPS:
            continue
        dist = getattr(ep, "dist", None)
        name = dist_name(dist)
        if not name:
            continue
        grouped.setdefault(name, []).append(ep)
        dists.setdefault(name, dist)

    return tuple(status_for(name, dists[name], eps) for name, eps in sorted(grouped.items()))
