"""Every migration in the canonical manifest must ship in the wheel.

The force-include block in pyproject.toml was a hand-maintained per-file list,
one line per migration, and it had silently fallen 10 files behind: 081-089 plus
095. Those are not incidental — 081 is the tenant RLS backstop, 084 feature
flags, 086 user permissions. A wheel-based deploy would come up with row-level
security absent and no error anywhere, because the runner only applies what it
can find.

Enumeration drifts. This test makes the drift fail the build instead of the
production database.
"""

from __future__ import annotations

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


def _force_include() -> dict[str, str]:
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    return cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]


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
