"""Every migration in the canonical manifest must ship in the wheel — and no
untracked instance file ever does.

The force-include block in pyproject.toml was a hand-maintained per-file list,
one line per migration, and it had silently fallen 10 files behind: 081-089 plus
095. Those are not incidental — 081 is the tenant RLS backstop, 084 feature
flags, 086 user permissions. A wheel-based deploy would come up with row-level
security absent and no error anywhere, because the runner only applies what it
can find.

Enumeration drifts. This test makes the drift fail the build instead of the
production database.

The other direction is just as dangerous: hatchling's force-include walks a
directory raw and never consults `exclude` config or .gitignore for it (see
hatch_build.py's module docstring for the empirical proof). crm/migrations and
infra/migrations are platform trees that also happen to be where this
instance's untracked, gitignored Delphi migrations live on disk. A directory-
level force-include would sweep those into any wheel built locally.
hatch_build.py replaces the static directory entries for those two trees with
a computed, filtered, per-file map; TestInstanceFilesNeverShip below pins that
the filtering is real, not just present.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import subprocess
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _manifest_entries() -> list[str]:
    path = REPO / "robothor" / "migrations" / "manifest.txt"
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _hook_module():
    spec = importlib.util.spec_from_file_location("hatch_build", REPO / "hatch_build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _force_include() -> dict[str, str]:
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    include = dict(cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"])
    hook = _hook_module()
    for source, target in hook.compute_migration_force_include(REPO).items():
        include[str(Path(source).relative_to(REPO))] = target
    return include


def _covered(source_path: str, include: dict[str, str]) -> bool:
    """A file ships if it is named directly or lives under an included dir."""
    if source_path in include:
        return True
    return any(
        source_path.startswith(f"{key.rstrip('/')}/")
        for key in include
        if "." not in Path(key).name
    )


class TestEveryMigrationShips:
    def test_no_manifest_entry_is_missing_from_the_wheel(self):
        include = _force_include()
        missing = []
        for entry in _manifest_entries():
            family, _, name = entry.partition("/")
            source = f"{family}/migrations/{name}"
            if not (REPO / source).exists():
                continue  # covered by test_manifest_files_exist
            if not _covered(source, include):
                missing.append(entry)
        assert missing == [], (
            f"{len(missing)} migrations are in the manifest but would not ship in a "
            f"wheel: {missing}"
        )

    def test_manifest_files_exist_on_disk(self):
        # The mirror failure: an entry naming a file that was never written
        # makes the runner stop dead mid-chain.
        missing = []
        for entry in _manifest_entries():
            family, _, name = entry.partition("/")
            if not (REPO / family / "migrations" / name).exists():
                missing.append(entry)
        assert missing == [], f"manifest names files that do not exist: {missing}"

    def test_platform_migrations_on_disk_are_in_the_manifest(self):
        # The other direction: a platform migration written but never registered
        # never runs, which is how 071 and 085 sat unapplied for months.
        #
        # Instance-local migrations are the deliberate exception. The delphi
        # schema belongs to this instance, not the platform, and the runner
        # treats anything outside the canonical manifest as unmanaged (#222).
        # Keying the exemption on the name rather than on absence-from-manifest
        # matters: "not in the manifest" would make this test vacuous, since
        # that is the very condition it exists to catch.
        entries = {e.partition("/")[2] for e in _manifest_entries()}
        on_disk = {p.name for p in (REPO / "crm" / "migrations").glob("[0-9]*.sql")}
        unregistered = sorted(name for name in on_disk - entries if "delphi" not in name.lower())
        assert unregistered == [], (
            f"platform migrations exist on disk but are not in the manifest, so "
            f"they will never run: {unregistered}"
        )


class TestInstanceFilesNeverShip:
    """Tripwire for the local-build wheel-packaging hole (fix/wheel-instance-excludes).

    hatchling's `recurse_forced_files` never calls `path_is_excluded` — a
    force-included directory ignores both `[tool.hatch.build.targets.wheel]
    exclude` and .gitignore. Excludes only apply to the normal
    include/only-include walk. Confirmed by reading hatchling's
    builders/plugin/interface.py: recurse_forced_files only skips its own
    fixed EXCLUDED_FILES/EXCLUDED_DIRECTORIES, nothing configurable. So the
    filter has to live in code that IS consulted — hatch_build.py's
    compute_migration_force_include — not in a pyproject `exclude` key.
    """

    def test_instance_patterns_are_configured_and_cover_delphi(self):
        hook = _hook_module()
        assert hook.INSTANCE_FILE_PATTERNS, "no instance-file exclude patterns configured"
        assert any(
            fnmatch.fnmatch("091_delphi_lip_xvenue.sql".lower(), pattern)
            for pattern in hook.INSTANCE_FILE_PATTERNS
        ), "delphi migrations are not covered by INSTANCE_FILE_PATTERNS"

    def test_both_force_included_migration_trees_are_hook_managed(self):
        # crm/migrations AND infra/migrations must go through the filtered
        # path — pyproject.toml must not also declare either as a static,
        # unfiltered directory force-include (that would ship everything on
        # disk regardless of what this hook computes).
        hook = _hook_module()
        assert set(hook.MIGRATION_FORCE_INCLUDE_DIRS) == {"crm/migrations", "infra/migrations"}
        cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
        static_include = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        for source_dir in hook.MIGRATION_FORCE_INCLUDE_DIRS:
            assert source_dir not in static_include, (
                f"{source_dir} is still a static, unfiltered force-include entry "
                "in pyproject.toml — it bypasses the hook's exclude filtering"
            )

    def test_hook_is_registered_for_the_wheel_target(self):
        # The hook only filters if hatchling actually loads it. Every other
        # test here imports hatch_build.py directly, so deleting the
        # [tool.hatch.build.targets.wheel.hooks.custom] registration from
        # pyproject.toml would keep this whole file green while wheels ship
        # with ZERO migrations (the static directory entries are gone too) —
        # the exact silent-RLS-absent failure this file exists to prevent.
        cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
        hooks = cfg["tool"]["hatch"]["build"]["targets"]["wheel"].get("hooks", {})
        assert hooks.get("custom", {}).get("path") == "hatch_build.py", (
            "hatch_build.py is not registered as the wheel target's custom build "
            "hook — wheels would ship with no migrations at all"
        )
        # And NOT build-wide: a build-wide hook also runs for the sdist, where
        # hatchling relocates force-included sources (crm/migrations/*.sql
        # would move to robothor/migrations/ inside the sdist).
        assert "custom" not in cfg["tool"]["hatch"]["build"].get("hooks", {}), (
            "the custom hook must be scoped to the wheel target, not build-wide"
        )

    def test_sdist_excludes_cover_delphi_in_both_migration_trees(self):
        # The wheel hook cannot protect the sdist (force_include only adds
        # files) and .gitignore only enumerates today's delphi migration
        # numbers, so a future 092_delphi_*.sql would ship in the sdist via
        # the normal walk. The sdist exclude patterns are the guard; pin that
        # they exist and actually match delphi names in both trees, any case.
        cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
        patterns = cfg["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
        for probe in (
            "crm/migrations/092_delphi_future.sql",
            "crm/migrations/055_DELPHI_case.sql",
            "infra/migrations/010_delphi_infra.sql",
        ):
            assert any(fnmatch.fnmatch(probe, p) for p in patterns), (
                f"sdist exclude patterns {patterns} do not cover {probe}"
            )

    def test_computed_force_include_matches_git_tracked_files_exactly(self):
        # The strongest cheap assertion available: what the hook would ship
        # for these two trees must be EXACTLY the set of files git tracks
        # there. Not a superset (an untracked instance file leaking through —
        # this box has 12 of those in crm/migrations right now) and not a
        # subset (a real platform migration silently dropped). Delete
        # INSTANCE_FILE_PATTERNS on a box with untracked instance SQL present
        # and this fails immediately.
        hook = _hook_module()
        computed = hook.compute_migration_force_include(REPO)
        shipped = {str(Path(source).relative_to(REPO)) for source in computed}

        tracked: set[str] = set()
        for source_dir in hook.MIGRATION_FORCE_INCLUDE_DIRS:
            result = subprocess.run(
                ["git", "ls-files", source_dir],
                cwd=REPO,
                capture_output=True,
                text=True,
                check=True,
            )
            tracked.update(line for line in result.stdout.splitlines() if line)

        assert shipped == tracked, (
            f"leaked (shipped but not git-tracked): {sorted(shipped - tracked)}; "
            f"dropped (tracked but not shipped): {sorted(tracked - shipped)}"
        )
