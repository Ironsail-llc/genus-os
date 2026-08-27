"""The source distribution must not ship the frontend's build output.

Measured on genusos 1.56.0 before this fix: 1,026 of 2,970 files in the sdist
were app/.next — 35% of the package, ~36 MB uncompressed, and 90 MB on disk.
All of it Next.js compiled chunks and .js.map source maps. Build output, not
source, and every one of those files is git-ignored.

They shipped anyway because the ignore lives in app/.gitignore, a NESTED
ignore file. hatchling's sdist is "everything not VCS-ignored", and it
consults the repository-root .gitignore — a nested one does not reach it. So
`git check-ignore app/.next` says IGNORED while the sdist includes it.

That is the whole defect: an exclusion that is real to git and invisible to
the packager. It matters now because `pip install genusos` is about to become
the way people get this, and the first thing a stranger downloads should not
be a stale frontend build.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

#: Paths that are build output or vendored dependencies. None may ship.
_MUST_NOT_SHIP = (
    "app/.next/server/chunks/x.js.map",
    "app/.next/standalone/server.js",
    "app/.next/static/chunk.js",
    "app/.next/cache/thing",
    "app/node_modules/react/index.js",
    "app/tsconfig.tsbuildinfo",
)

#: Real source that must still ship, so the fix cannot be "exclude app/".
_MUST_SHIP = (
    "app/src/components/Panel.tsx",
    "app/package.json",
    "app/next.config.js",
)


def _sdist_excludes() -> list[str]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return (
        data.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("sdist", {})
        .get("exclude", [])
    )


def _excluded(path: str, patterns: list[str]) -> bool:
    """Does any pattern cover `path`?

    Deliberately no pathspec import. It is available here only as a transitive
    dependency and is not declared in pyproject, so a CI image without it would
    error — and `importorskip` would let this guard skip silently, which is
    exactly how a test stops testing anything.

    Only the shapes actually used in the exclude list are handled: a rooted
    prefix (`/app/.next`) and a bare glob. Anything else raises rather than
    quietly returning False, so a future pattern cannot slip past unchecked.
    """
    from fnmatch import fnmatch

    for raw in patterns:
        pattern = raw.rstrip("/")
        if pattern.startswith("/"):
            prefix = pattern[1:]
            if path == prefix or path.startswith(prefix + "/"):
                return True
        elif "*" in pattern or "?" in pattern:
            if fnmatch(path, pattern) or any(fnmatch(part, pattern) for part in path.split("/")):
                return True
        elif path == pattern or path.startswith(pattern + "/"):
            return True
    return False


def test_the_scan_root_is_real():
    """A wrong root makes every assertion below vacuous."""
    assert (_ROOT / "pyproject.toml").is_file()
    assert _sdist_excludes(), "no sdist exclude list found at all"


def test_build_output_is_excluded():
    patterns = _sdist_excludes()
    shipped = [p for p in _MUST_NOT_SHIP if not _excluded(p, patterns)]
    assert not shipped, (
        f"build output would ship in the sdist: {shipped}. These are git-ignored "
        "via app/.gitignore, which hatchling does not consult — the exclusion has "
        "to be stated here as well."
    )


def test_real_frontend_source_still_ships():
    """The fix must be surgical: excluding all of app/ would be wrong."""
    patterns = _sdist_excludes()
    dropped = [p for p in _MUST_SHIP if _excluded(p, patterns)]
    assert not dropped, f"real source excluded from the sdist: {dropped}"
