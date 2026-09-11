"""Every migration-count claim in the published docs must match the manifest.

`robothor/migrations/manifest.txt` is the canonical migration chain. Prose
that hand-counts it drifts the moment a migration lands: README.md advertised
"83 checksum-verified migrations" against a 113-entry manifest for months, and
nothing failed. A number in prose is a claim about the tree, so it gets the
same treatment as any other claim -- it is checked.

The fix for a failure here is normally to DELETE the number, not to update it.
`robothor migrate --check` prints the real count against a live database.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "robothor" / "migrations" / "manifest.txt"

# Every shape the docs have used to state the count. Each exists because the
# tree carried it: helm/genus-os/README.md said "the sole packaged 83-entry
# manifest", and docs/PRODUCTION_HARDENING_TODO.md said "the canonical 83 SQL
# files" -- neither of which the first pattern sees.
COUNT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(\d+)\s+(?:checksum-verified\s+|canonical\s+)?migrations\b"),
    re.compile(r"\b(\d+)-entry\s+(?:migration\s+)?manifest\b"),
    re.compile(r"\b(\d+)\s+(?:canonical\s+)?SQL\s+files\b"),
)


def manifest_migration_count() -> int:
    """Non-comment, non-blank lines in the canonical migration manifest."""
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    return sum(
        1 for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )


# Dated session artifacts (design specs, plans) describe the repo as it WAS,
# including the very drift this test exists to catch — "README says 83
# migrations" is a true sentence about 2026-09-10. Rewriting history to satisfy
# a gate would make the record less true, so they are exempt here exactly as
# they are in scripts/check_doc_commands.py and scripts/check_doc_links.py.
ARCHIVED_DOC_DIRS = ("docs/superpowers/",)


def documents() -> list[Path]:
    """README.md, the chart README, and every live markdown page under docs/."""
    paths = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "helm" / "genus-os" / "README.md",
    ]
    paths.extend(
        path
        for path in sorted((REPO_ROOT / "docs").rglob("*.md"))
        if not str(path.relative_to(REPO_ROOT)).startswith(ARCHIVED_DOC_DIRS)
    )
    return [path for path in paths if path.is_file()]


def test_archived_design_docs_are_not_scanned() -> None:
    """A dated spec may quote the stale number it was written to fix."""
    scanned = {str(path.relative_to(REPO_ROOT)) for path in documents()}
    assert not any(name.startswith("docs/superpowers/") for name in scanned)


def test_manifest_is_readable_and_non_empty() -> None:
    assert manifest_migration_count() > 0, f"{MANIFEST_PATH} lists no migrations"


def test_documented_migration_counts_match_the_manifest() -> None:
    expected = manifest_migration_count()
    stale: list[str] = []

    for path in documents():
        rel = path.relative_to(REPO_ROOT)
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for pattern in COUNT_PATTERNS:
                for match in pattern.finditer(line):
                    claimed = int(match.group(1))
                    if claimed != expected:
                        stale.append(f"{rel}:{lineno}: claims {claimed}, manifest has {expected}")

    assert not stale, (
        "stale migration counts in prose (delete the number and point readers at "
        "`robothor migrate --check` instead):\n" + "\n".join(stale)
    )
