"""What a distribution DECLARES it will contribute, read before it is imported.

`ep.load()` executes the plugin's module body inside the daemon. Every check
the loader performs — contract version, reserved names, clashes — happens after
that, so a refused plugin has already run. This module is the smallest thing
that changes the order: a `genus-plugin.yaml` shipped in the distribution's
metadata, read from the packaging layer, never from the plugin's own code.

Deliberately NOT RFC #267's model (tiers, lockfile, signing bar, per-tenant
enablement). That design is blocked on five open operator questions and on a
second plugin author existing, and the shipped seam is not even the design the
RFC describes. This is the one property worth having first: an unmanifested
distribution is never imported.

Honest limit: a plugin that DOES ship a manifest must still be imported before
its exports can be compared to it — you cannot read a module's contributions
without executing it. The manifest is what makes those contributions reviewable
BEFORE install, and what the loader holds the payload to afterwards.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MANIFEST_NAME = "genus-plugin.yaml"


@dataclass(frozen=True)
class PluginManifest:
    """A distribution's declared intent."""

    name: str = ""
    contract_version: int | None = None
    declared: dict[str, set[str]] = field(default_factory=dict)

    def declares(self, kind: str) -> set[str]:
        return set(self.declared.get(kind) or set())


def read_manifest(dist: Any) -> PluginManifest | None:
    """The distribution's manifest, or None when it ships none.

    None means "refuse without importing". Reading is best-effort and never
    raises: a malformed manifest is the same as no manifest, because both mean
    the distribution has not said what it intends to do.
    """
    if dist is None:
        return None
    try:
        raw = dist.read_text(MANIFEST_NAME)
    except Exception:  # noqa: BLE001 - unreadable metadata is "undeclared"
        return None
    if not raw:
        return None
    try:
        import yaml

        data = yaml.safe_load(raw) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("Plugin manifest is not valid YAML (%s) — treating as undeclared", e)
        return None
    if not isinstance(data, dict):
        return None

    declared: dict[str, set[str]] = {}
    for kind, value in data.items():
        if kind in ("name", "contract_version"):
            continue
        if isinstance(value, list):
            declared[kind] = {str(v) for v in value if isinstance(v, str | int | float)}
    version = data.get("contract_version")
    return PluginManifest(
        name=str(data.get("name") or ""),
        contract_version=int(version) if isinstance(version, int) else None,
        declared=declared,
    )


def manifest_mode() -> str:
    """How strictly the manifest requirement is applied.

    A ladder, not a flip, because this is a BREAKING change to a shipped seam:
    every plugin published before the manifest existed would be refused on
    upgrade. `observe` records which distributions lack one without refusing
    them, so an operator can see the blast radius before it bites.

    The security property — refusing before arbitrary code executes — exists
    only in `enforce`. `observe` still imports; it just says so. That is worth
    stating plainly rather than letting a dashboard reading "observe" imply
    containment it does not have.
    """
    import os

    if os.environ.get("ROBOTHOR_PLUGIN_MANIFEST_ENABLED", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return "off"
    mode = os.environ.get("ROBOTHOR_PLUGIN_MANIFEST_MODE", "observe").strip().lower()
    return mode if mode in {"off", "observe", "enforce"} else "observe"
