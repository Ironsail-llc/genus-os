"""The release updater stamps every place a version is written down — install.sh too.

``scripts/install.sh`` carries the release it shipped with, so the published
one-liner installs that release even when the GitHub releases API cannot be
reached. A stamp nothing rewrites is worse than no stamp: it would pin the
published installer to whatever version happened to be in the tree the day the
file was written, for ever.

So three things are pinned here: the updater rewrites the stamp, the version
consistency checker fails when the stamp drifts, and ``.releaserc.js`` commits
the file it rewrote.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATER = REPO_ROOT / "scripts" / "update-helm-values.sh"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
CHECKER = REPO_ROOT / "scripts" / "check-version-consistency.js"
RELEASERC = REPO_ROOT / ".releaserc.js"

STAMP_RE = re.compile(r'^INSTALL_SH_DEFAULT_VERSION="(v[0-9]+\.[0-9]+\.[0-9]+)"$', re.MULTILINE)

#: Everything `scripts/update-helm-values.sh main` reads or rewrites.
RELEASE_FILES = (
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "app/package.json",
    "robothor/__init__.py",
    "helm/genus-os/Chart.yaml",
    "helm/genus-os/values-staging.yaml",
    "helm/genus-os/values-production.yaml",
    "scripts/install.sh",
    "scripts/update-helm-values.sh",
    "scripts/check-version-consistency.js",
)

node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@pytest.fixture
def release_tree(tmp_path: Path) -> Path:
    """A throwaway copy of exactly the files the updater touches."""
    for relative in RELEASE_FILES:
        source = REPO_ROOT / relative
        assert source.is_file(), f"{relative} is missing from the repository"
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return tmp_path


def test_install_sh_carries_a_stamp() -> None:
    assert STAMP_RE.search(INSTALL_SH.read_text(encoding="utf-8")), (
        "scripts/install.sh has no INSTALL_SH_DEFAULT_VERSION line for the updater to rewrite"
    )


def test_the_committed_stamp_matches_the_committed_version() -> None:
    stamp = STAMP_RE.search(INSTALL_SH.read_text(encoding="utf-8")).group(1)
    chart = re.search(
        r"^version:\s*\"?([^\"\s]+)\"?\s*$",
        (REPO_ROOT / "helm" / "genus-os" / "Chart.yaml").read_text(encoding="utf-8"),
        re.MULTILINE,
    ).group(1)
    assert stamp == f"v{chart}", f"install.sh is stamped {stamp}, the chart is {chart}"


@node
def test_the_updater_rewrites_the_stamp(release_tree: Path) -> None:
    subprocess.run(  # noqa: S603 - the script under test
        ["bash", "scripts/update-helm-values.sh", "9.9.9", "main"],
        cwd=str(release_tree),
        check=True,
        capture_output=True,
        text=True,
    )
    stamped = STAMP_RE.search((release_tree / "scripts" / "install.sh").read_text(encoding="utf-8"))
    assert stamped, "the updater removed the stamp"
    assert stamped.group(1) == "v9.9.9"

    # And every other version moved with it, which is what makes the stamp safe
    # to trust as a default.
    assert '"version": "9.9.9"' in (release_tree / "package.json").read_text(encoding="utf-8")
    assert "version: 9.9.9" in (release_tree / "helm" / "genus-os" / "Chart.yaml").read_text(
        encoding="utf-8"
    )


@node
def test_the_updater_leaves_the_stamp_alone_on_staging(release_tree: Path) -> None:
    before = (release_tree / "scripts" / "install.sh").read_text(encoding="utf-8")
    subprocess.run(  # noqa: S603 - the script under test
        ["bash", "scripts/update-helm-values.sh", "9.9.9", "staging"],
        cwd=str(release_tree),
        check=True,
        capture_output=True,
        text=True,
    )
    after = (release_tree / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert after == before, "a staging bump must not restamp the published installer"


@node
def test_a_drifted_stamp_fails_the_consistency_check(release_tree: Path) -> None:
    install_sh = release_tree / "scripts" / "install.sh"
    install_sh.write_text(
        STAMP_RE.sub('INSTALL_SH_DEFAULT_VERSION="v0.0.1"', install_sh.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 - the script under test
        ["node", "scripts/check-version-consistency.js"],
        cwd=str(release_tree),
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "GENUS_ALLOW_DEPLOYMENT_LAG": "true"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "a drifted install.sh stamp passed the consistency check"
    assert "install.sh" in (result.stdout + result.stderr)


def test_semantic_release_commits_the_file_it_stamped() -> None:
    body = RELEASERC.read_text(encoding="utf-8")
    assert "scripts/install.sh" in body, (
        "the release commit does not carry scripts/install.sh, so the stamp is rewritten "
        "and then thrown away"
    )


def test_the_updater_is_still_the_prepare_command() -> None:
    body = RELEASERC.read_text(encoding="utf-8")
    assert "scripts/update-helm-values.sh" in body
