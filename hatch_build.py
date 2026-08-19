"""Hatchling custom build hook: filtered force-include for migration trees.

The static ``[tool.hatch.build.targets.wheel.force-include]`` table walks an
entire directory with no way to leave a file out of it. hatchling's `exclude`
config option — and .gitignore — are never consulted for force-included
paths. Verified against hatchling's own source
(``hatchling/builders/plugin/interface.py::recurse_forced_files``): the
force-include walk only skips hatchling's own fixed ``EXCLUDED_FILES`` /
``EXCLUDED_DIRECTORIES`` sets (things like ``__pycache__`` and ``.DS_Store``).
It never calls ``config.path_is_excluded``, which is where both a configured
``exclude`` pattern and the VCS-ignore spec live. Adding an `exclude` pattern
to pyproject.toml for a force-included directory is therefore a silent no-op
— confirmed empirically by building a wheel with such a pattern in place and
finding the excluded file in it anyway.

``crm/migrations`` and ``infra/migrations`` are platform migration trees that
also double as the on-disk location for this instance's untracked, gitignored
Delphi trading-tenant migrations (``crm/migrations/053_delphi_*.sql`` and
friends — see ``.gitignore``). A directory-level force-include sweeps those
into any wheel built on a box where they happen to exist, even though CI's
clean-clone builds never see them (``robothor/tests/test_wheel_smoke.py``
greps CI-built wheels for this, which only proves CI is safe, not a local
build on a box with instance files present).

This hook recomputes the force-include mapping for those two trees at build
time as an explicit per-file map, filtering out anything matching
``INSTANCE_FILE_PATTERNS``. Filtering happens in code hatchling actually
consults (the ``build_data["force_include"]`` a build hook returns, merged in
after ``initialize()`` runs) instead of a config key it ignores for forced
paths. ``robothor/tests/test_migration_packaging.py`` pins that the filtering
is real.

``templates`` (the init scaffold) stays a plain static force-include entry in
pyproject.toml — checked on this box via ``git status --ignored -- templates``
and clean, no instance files under it.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# Case-insensitive glob patterns. Any filename under a tree listed in
# MIGRATION_FORCE_INCLUDE_DIRS that matches one of these never enters a wheel,
# whether it is tracked or not. Extend this list — not the directories below —
# if another instance-only migration prefix shows up.
INSTANCE_FILE_PATTERNS: tuple[str, ...] = ("*delphi*",)

# source dir (repo-root-relative) -> target dir inside the wheel. Mirrors the
# layout robothor/db/migrate.py expects at runtime
# (_PACKAGE_MIGRATIONS_DIR / "crm" and / "infra").
MIGRATION_FORCE_INCLUDE_DIRS: dict[str, str] = {
    "crm/migrations": "robothor/migrations/crm",
    "infra/migrations": "robothor/migrations/infra",
}


def _is_instance_file(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in INSTANCE_FILE_PATTERNS)


def compute_migration_force_include(root: Path) -> dict[str, str]:
    """Explicit {absolute_source_path: target_path} map, instance files filtered out."""
    force_include: dict[str, str] = {}
    for source_dir, target_dir in MIGRATION_FORCE_INCLUDE_DIRS.items():
        source_root = root / source_dir
        if not source_root.is_dir():
            continue
        for path in sorted(source_root.iterdir()):
            if not path.is_file() or _is_instance_file(path.name):
                continue
            force_include[str(path)] = f"{target_dir}/{path.name}"
    return force_include


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        build_data.setdefault("force_include", {}).update(
            compute_migration_force_include(Path(self.root))
        )
