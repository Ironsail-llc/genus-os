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
real loader with the real reserved names — ``loader.builtin_names(group)``,
derived from the same live registries the enforcing callers read. That is what
makes ``disabled``, ``failed`` and the reason string here mean exactly what
they mean in the daemon. The first version passed ``reserved_names=set()``, so
a distribution the engine refuses for shadowing ``web_fetch`` was reported as
``loaded`` by every operator surface and passed a ``required`` doctor check
whose whole purpose is to catch that case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.plugins.lockfile import LockRow

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
    #: The lock row's ``source`` -- where this platform got the distribution --
    #: or None for anything it did not install. The ABSENCE is what matters:
    #: ``genus plugin remove`` refuses a row with no source, because
    #: uninstalling a package somebody else put on the box, on the grounds that
    #: it appears in this platform's lockfile, is reaching outside what the
    #: platform owns. An operator surface that could not tell the two apart
    #: would offer a button whose only outcome is a 422.
    source: dict[str, Any] | None = None
    #: What install and sync established about this distribution, composed for
    #: the Helm's trust badge. See :func:`_trust`.
    trust: dict[str, Any] = field(default_factory=dict)

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
            "source": self.source,
            "trust": self.trust,
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


def _trust(row: LockRow | None, drifted: bool, manifest: dict[str, Any] | None) -> dict[str, Any]:
    """What the record says about whether to trust this distribution.

    Read back, never re-derived. The index signature was verified when the
    index was parsed -- the lock row carries the key id only because that check
    passed -- and the wheel's sha256 was compared to the index's pin when it was
    downloaded, before pip ran; a mismatch refuses the install, so a recorded
    hash is one that matched. Nothing here fetches, hashes a wheel, or checks a
    signature, so a listing is as cheap as it was.

    ``signature.verified`` needs a ``registry`` origin AND a key id: a wheel
    installed from disk with ``--sha256`` is pinned, but nobody vouched for who
    published it. ``manifest.matches`` is None with no row, because there is
    nothing recorded to match against. No path: ``index_url`` is a URL the
    operator configured, which the listing's ``indexes`` already carries.
    """
    if row is None:
        return {
            "signature": {"verified": False, "key_id": None, "index_url": None},
            "wheel": {"sha256": None, "pinned": False},
            "manifest": {"sha256": None, "matches": None},
            "scan": {"verdict": None, "reasons": [], "members_accounted": None},
            "lockfile": {"recorded": False, "drifted": False},
            "contract_version": (manifest or {}).get("contract_version"),
        }
    source = row.source
    signed = bool(source is not None and source.origin == "registry" and source.publisher_key_id)
    return {
        "signature": {
            "verified": signed,
            "key_id": source.publisher_key_id if signed and source else None,
            "index_url": (source.index_url or None) if signed and source else None,
        },
        "wheel": {"sha256": row.dist_sha256 or None, "pinned": bool(row.dist_sha256)},
        "manifest": {"sha256": row.manifest_sha256 or None, "matches": not drifted},
        "scan": {
            "verdict": row.verdict,
            "reasons": list(row.scan_reasons),
            "members_accounted": row.members_accounted or None,
        },
        "lockfile": {"recorded": True, "drifted": drifted},
        "contract_version": (manifest or {}).get("contract_version"),
    }


def status_for(name: str, dist: Any, eps: list[Any]) -> PluginStatus:
    """Measure one distribution by loading exactly its entry points."""
    from robothor.plugins import lockfile
    from robothor.plugins.loader import load_plugins

    lock = lockfile.read_lockfile()
    row = lock.row(name)
    # No `reserved_names`: the default is the per-group built-in set, which is
    # what every enforcing caller passes. Passing `set()` here is what made a
    # distribution the engine refuses for shadowing `web_fetch` report as
    # `loaded` on every operator surface, the `required` doctor check included.
    loaded = load_plugins(entry_points=eps)

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

    drifted = bool(row is not None and row.manifest_sha256 != lockfile.manifest_digest(dist))
    manifest = _manifest_json(dist)
    return PluginStatus(
        name=name,
        version=str(getattr(dist, "version", "") or ""),
        groups=tuple(sorted({getattr(ep, "group", "") for ep in eps} - {""})),
        state=state,
        recorded=row is not None,
        enabled=row.enabled if row is not None else True,
        verdict=row.verdict if row is not None else "",
        drifted=drifted,
        failure_reason=reason,
        contributions=contributions,
        manifest=manifest,
        source=row.source.as_json() if row is not None and row.source is not None else None,
        trust=_trust(row, drifted, manifest),
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
