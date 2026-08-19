"""Every hatchling force-include source must reach the Docker build context.

The v1.30.1 release failed exactly here: pyproject gained a force-include of
templates/ while .dockerignore still excluded it, so the image's editable
install died with 'Forced include not found'. CI builds from the git checkout
and cannot see .dockerignore effects — this test is the tripwire.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _force_include_sources() -> list[str]:
    with (REPO / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    hatch = data.get("tool", {}).get("hatch", {})
    build = hatch.get("build", {})
    sources: list[str] = []
    for target in build.get("targets", {}).values():
        sources.extend(target.get("force-include", {}).keys())
    sources.extend(build.get("force-include", {}).keys())
    return sources


def _dockerignored(path: str) -> bool:
    patterns = []
    for line in (REPO / ".dockerignore").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("!"):
            patterns.append(line.rstrip("/"))
    top = path.split("/")[0]
    return any(p in (path, top) for p in patterns)


def test_force_include_sources_survive_dockerignore():
    sources = _force_include_sources()
    assert sources, "expected at least one force-include source"
    blocked = [s for s in sources if _dockerignored(s)]
    assert not blocked, (
        f"force-include sources excluded from the Docker build context: {blocked} — "
        "the image build will fail with 'Forced include not found'"
    )


def _build_hook_scripts() -> list[str]:
    """Build-hook scripts hatchling needs at install time (v1.30.13 died when
    hatch_build.py was referenced by [tool.hatch.build.hooks.custom] but never
    COPYied into the image)."""
    with (REPO / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    hatch = data.get("tool", {}).get("hatch", {})
    build = hatch.get("build", {})
    scripts: list[str] = []
    hook_tables = [build.get("hooks", {})]
    for target in build.get("targets", {}).values():
        hook_tables.append(target.get("hooks", {}))
    for hooks in hook_tables:
        custom = hooks.get("custom")
        if custom is not None:
            scripts.append(custom.get("path", "hatch_build.py"))
    return scripts


def test_build_hook_scripts_reach_the_python_image():
    scripts = _build_hook_scripts()
    assert scripts, "expected the wheel-filter build hook from #258 to be declared"
    blocked = [s for s in scripts if _dockerignored(s)]
    assert not blocked, f"build-hook scripts excluded by .dockerignore: {blocked}"
    dockerfile = (REPO / "Dockerfile.python").read_text()
    copied = {
        part.rstrip("/")
        for line in dockerfile.splitlines()
        if line.startswith("COPY ") and "--from" not in line
        for part in line.split()[1:-1]
    }
    missing = [s for s in scripts if s not in copied]
    assert not missing, (
        f"build-hook scripts never COPYied into the python image: {missing} — "
        "uv pip install fails with 'Build script does not exist'"
    )


def test_force_include_sources_are_copied_into_the_python_image():
    """.dockerignore admission is not enough — Dockerfile.python COPYies
    selectively, so a force-include source absent from its COPY list still
    fails the in-image install (v1.30.2 died exactly here on templates/)."""
    dockerfile = (REPO / "Dockerfile.python").read_text()
    copied = {
        line.split()[1].rstrip("/")
        for line in dockerfile.splitlines()
        if line.startswith("COPY ") and "--from" not in line
    }
    missing = [
        s
        for s in _force_include_sources()
        if s.split("/")[0] not in copied and s.rstrip("/") not in copied
    ]
    assert not missing, (
        f"force-include sources never COPYied into the python image: {missing}"
    )
