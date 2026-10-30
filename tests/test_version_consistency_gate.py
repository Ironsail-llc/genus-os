"""The version gate must not block the pull request that repairs the release.

This deadlock has now happened twice, from two different causes:

* v1.57.0 on 2026-08-27 — the promotion push lost a race with four merges, so
  every version field said 1.57.0 while ``values-production.yaml`` still pinned
  v1.56.0. ``tests/test_promote_release.py`` covers that cause: the push now
  retries.
* v1.108.2 on 2026-10-09 — the tag was cut and the release published, then the
  image build failed. No v1.108.2 image was ever pushed, so production lawfully
  lagged at v1.108.1 and no retry could have helped.

Both times the gate failed every pull request in the repository, including the
one that would have fixed the pipeline, because it asked a pull request a
question only a release can answer: *does the deployed tag equal the version we
claim to be?* Nothing in a PR's diff can publish an image.

So the question is now asked per event. A release run still enforces the no-lag
invariant, with the operator hatch for recovery releases. A pull request is
asked what it can answer instead — *did you change the tag?* — which keeps
hand-edits caught without the deadlock.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check-version-consistency.js"
PRODUCTION_VALUES = "helm/genus-os/values-production.yaml"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _seed(tmp_path: Path, *, production_tag: str, version: str) -> Path:
    """A repo carrying the real script and just enough metadata to check.

    Every field the gate reads has to agree on ``version`` or the metadata half
    fails for an unrelated reason and the test proves nothing.
    """
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    (work / "helm" / "genus-os").mkdir(parents=True)
    (work / "robothor").mkdir()
    (work / "app").mkdir()

    (work / "scripts" / SCRIPT.name).write_text(SCRIPT.read_text())
    (work / "scripts" / "install.sh").write_text(f'INSTALL_SH_DEFAULT_VERSION="v{version}"\n')
    (work / "pyproject.toml").write_text(f'[project]\nname = "genusos"\nversion = "{version}"\n')
    (work / "robothor" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (work / "uv.lock").write_text(f'[[package]]\nname = "genusos"\nversion = "{version}"\n')
    (work / "package.json").write_text(json.dumps({"version": version}))
    (work / "package-lock.json").write_text(
        json.dumps({"version": version, "packages": {"": {"version": version}}})
    )
    (work / "app" / "package.json").write_text(json.dumps({"version": version}))
    (work / "helm" / "genus-os" / "Chart.yaml").write_text(
        f'version: {version}\nappVersion: "{version}"\n'
    )
    (work / PRODUCTION_VALUES).write_text(f'global:\n  imageTag: "v{production_tag}"\n')
    # An empty holder marks staging idle, which puts its tag in scope too.
    (work / "helm" / "genus-os" / "values-staging.yaml").write_text(
        f'global:\n  imageTag: "v{production_tag}"\n  deployedFromPR: ""\n'
    )

    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "gate@test")
    _git(work, "config", "user.name", "gate")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "seed")
    return work


def _run(work: Path, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    import os

    env = {**os.environ, **env_overrides}
    return subprocess.run(
        ["node", f"scripts/{SCRIPT.name}"],
        cwd=work,
        capture_output=True,
        text=True,
        env=env,
    )


def test_a_lagging_release_run_still_fails(tmp_path: Path) -> None:
    """The invariant this gate exists for is untouched on release runs."""
    work = _seed(tmp_path, production_tag="1.108.1", version="1.108.2")
    result = _run(work)
    assert result.returncode != 0
    assert "product version drift" in result.stderr


def test_a_lagging_release_run_does_not_advise_promoting_blindly(tmp_path: Path) -> None:
    """Following the old advice on a failed image build pins an unpullable tag.

    The recovery text has to send the reader to check whether the image exists
    before promoting, and name the do-not-promote case explicitly.
    """
    work = _seed(tmp_path, production_tag="1.108.1", version="1.108.2")
    result = _run(work)
    assert "was actually published" in result.stderr
    assert "Do NOT promote" in result.stderr


def test_the_operator_hatch_lets_a_recovery_release_through(tmp_path: Path) -> None:
    work = _seed(tmp_path, production_tag="1.108.1", version="1.108.2")
    result = _run(work, GENUS_ALLOW_DEPLOYMENT_LAG="true")
    assert result.returncode == 0, result.stderr
    assert "promotion pending" in result.stdout


def test_a_pull_request_is_not_blocked_by_someone_elses_stalled_release(
    tmp_path: Path,
) -> None:
    """The regression. A PR that touches no tag passes while the lag persists."""
    work = _seed(tmp_path, production_tag="1.108.1", version="1.108.2")
    (work / "README.md").write_text("an unrelated change\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "docs: unrelated")
    result = _run(work, GENUS_VERSION_BASELINE_REF="HEAD~1")
    assert result.returncode == 0, result.stderr


def test_a_pull_request_may_not_hand_edit_the_deployment_tag(tmp_path: Path) -> None:
    """Dropping the lag check on PRs must not drop tag coverage with it."""
    work = _seed(tmp_path, production_tag="1.108.1", version="1.108.2")
    (work / PRODUCTION_VALUES).write_text('global:\n  imageTag: "v1.108.2"\n')
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "chore: hand-edit the tag")
    result = _run(work, GENUS_VERSION_BASELINE_REF="HEAD~1")
    assert result.returncode != 0
    assert "must not change the deployment image tag" in result.stderr
    assert "1.108.1 -> 1.108.2" in result.stderr


def test_an_unreachable_baseline_fails_instead_of_verifying_nothing(tmp_path: Path) -> None:
    """A comparison against a ref that cannot be read proves nothing, so it
    must not quietly succeed."""
    work = _seed(tmp_path, production_tag="1.108.1", version="1.108.2")
    result = _run(work, GENUS_VERSION_BASELINE_REF="origin/does-not-exist")
    assert result.returncode != 0
    assert "does-not-exist" in result.stderr


@pytest.mark.parametrize("field", ["robothor/__init__.py", "package.json"])
def test_metadata_drift_still_fails_on_a_pull_request(tmp_path: Path, field: str) -> None:
    """Only the deployment half is event-scoped. Internal metadata consistency
    is a property of the diff, so a PR is still held to it."""
    work = _seed(tmp_path, production_tag="1.108.1", version="1.108.2")
    path = work / field
    path.write_text(re.sub(r"1\.108\.2", "1.108.0", path.read_text()))
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "chore: skew a version")
    result = _run(work, GENUS_VERSION_BASELINE_REF="HEAD~1")
    assert result.returncode != 0
    assert "product version drift" in result.stderr
