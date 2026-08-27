"""The promotion push must survive main moving under it.

On 2026-08-27 the release build cut v1.57.0 at 23:58, built and scanned
both images, and pushed the GitOps bump at 00:35. In those 35 minutes four
PRs merged, so ``git push origin HEAD:main`` was rejected non-fast-forward.
There was no retry, so the promotion was simply lost: every version field
in the repo said 1.57.0 while ``values-production.yaml`` still pinned
v1.56.0.

That drift then deadlocked the repository. ``Production release gate`` is
the first job in the release workflow and fails on exactly this lag, which
skips every downstream job — including the promotion that is the only
thing able to clear it. The gate blocked the fix for the condition it was
gating on, and no PR could merge until someone pushed to main by hand.

A release that takes half an hour to build will always race the merges
landing behind it. The push has to expect that.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "promote-release.sh"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    """A bare origin plus a working clone, both carrying the real scripts."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)

    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")

    # Everything check-version-consistency.js reads, all agreeing on the
    # released version — only the GitOps tags lag, which is the real shape.
    helm = work / "helm" / "genus-os"
    helm.mkdir(parents=True)
    (helm / "values-production.yaml").write_text('global:\n  imageTag: "v1.56.0"\n')
    (helm / "values-staging.yaml").write_text(
        'global:\n  imageTag: "v1.56.0"\n  deployedFromPR: ""\n'
    )
    (helm / "Chart.yaml").write_text('version: 1.57.0\nappVersion: "1.57.0"\n')
    (work / "pyproject.toml").write_text('[project]\nname = "genusos"\nversion = "1.57.0"\n')
    (work / "package.json").write_text('{"version": "1.57.0"}\n')
    (work / "package-lock.json").write_text(
        '{"version": "1.57.0", "packages": {"": {"version": "1.57.0"}}}\n'
    )
    (work / "app").mkdir()
    (work / "app" / "package.json").write_text('{"version": "1.57.0"}\n')
    (work / "robothor").mkdir()
    (work / "robothor" / "__init__.py").write_text('__version__ = "1.57.0"\n')
    (work / "uv.lock").write_text('[[package]]\nname = "genusos"\nversion = "1.57.0"\n')
    scripts = work / "scripts"
    scripts.mkdir()
    for name in ("promote-release-values.js", "check-version-consistency.js"):
        src = REPO_ROOT / "scripts" / name
        if src.exists():
            (scripts / name).write_text(src.read_text())
    (scripts / "promote-release.sh").write_text(SCRIPT.read_text())
    (scripts / "promote-release.sh").chmod(0o755)

    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "seed")
    _git(work, "push", "-q", "origin", "main")
    return origin, work


def _land_a_concurrent_merge(tmp_path: Path, origin: Path, name: str = "merger") -> None:
    """Someone else's PR merges while the release build is still building."""
    other = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "o")
    (other / "UNRELATED.md").write_text("a PR that merged during the build\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "feat: something else entirely")
    _git(other, "push", "-q", "origin", "main")


def _run(work: Path, version: str = "1.57.0") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/promote-release.sh", version],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_script_exists_and_is_executable():
    assert SCRIPT.exists(), "scripts/promote-release.sh missing"
    assert SCRIPT.stat().st_mode & 0o111


def test_promotes_cleanly_when_nothing_else_moved(tmp_path: Path):
    origin, work = _seed(tmp_path)
    result = _run(work)
    assert result.returncode == 0, result.stderr
    assert 'imageTag: "v1.57.0"' in (work / "helm/genus-os/values-production.yaml").read_text()


def test_survives_a_merge_landing_during_the_build(tmp_path: Path):
    """The incident, verbatim: main moved between checkout and push."""
    origin, work = _seed(tmp_path)
    _land_a_concurrent_merge(tmp_path, origin)

    result = _run(work)
    assert result.returncode == 0, f"promotion lost to the race:\n{result.stderr}"

    # Read main back out of the bare origin — the promotion must be there.
    check = tmp_path / "check"
    subprocess.run(["git", "clone", "-q", str(origin), str(check)], check=True)
    assert 'imageTag: "v1.57.0"' in (check / "helm/genus-os/values-production.yaml").read_text()
    # ...and it must not have clobbered the merge it raced.
    assert (check / "UNRELATED.md").exists(), "the concurrent merge was overwritten"


def test_already_promoted_is_not_an_error_after_a_race(tmp_path: Path):
    """A concurrent run that promoted first is success, not a failed deploy."""
    origin, work = _seed(tmp_path)
    # An unrelated merge first, so the other run's promotion sits on a
    # different parent. Without it both runs build a byte-identical commit
    # — same tree, same message, and the script normalises the author — which
    # hashes to the same SHA and pushes as a trivial fast-forward, so no race
    # is exercised at all.
    _land_a_concurrent_merge(tmp_path, origin)

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "o")
    subprocess.run(
        ["bash", "scripts/promote-release.sh", "1.57.0"], cwd=other, check=True, timeout=120
    )

    result = _run(work)
    assert result.returncode == 0, result.stderr
    assert "already promoted" in (result.stdout + result.stderr).lower()


def test_a_genuine_noop_on_the_first_attempt_still_fails(tmp_path: Path):
    """Promoting a version already pinned, with no race, stays an error.

    The original step refused an ambiguous no-op on purpose: it means the
    release output and the values disagree about what is being deployed.
    Making the retry path tolerant must not make that silent.
    """
    origin, work = _seed(tmp_path)
    assert _run(work).returncode == 0
    second = _run(work)
    assert second.returncode != 0
    assert "already promoted" in (second.stdout + second.stderr).lower()


def test_recovers_without_a_remote_tracking_ref(tmp_path: Path):
    """actions/checkout configures a narrow refspec.

    The retry must not depend on ``origin/<branch>`` existing — in CI it may
    never have been created. This deletes the remote-tracking ref before the
    race, which is the closest local stand-in for that checkout.
    """
    origin, work = _seed(tmp_path)
    _land_a_concurrent_merge(tmp_path, origin)
    # Drop the fetch refspec as well as the ref. Deleting the ref alone is
    # not a reproduction: a normal clone carries
    # `+refs/heads/*:refs/remotes/origin/*`, so the next fetch just recreates
    # it and the test passes against the broken code too.
    subprocess.run(["git", "config", "--unset-all", "remote.origin.fetch"], cwd=work, check=True)
    subprocess.run(["git", "update-ref", "-d", "refs/remotes/origin/main"], cwd=work, check=True)

    result = _run(work)
    assert result.returncode == 0, f"retry needed a tracking ref:\n{result.stderr}"

    check = tmp_path / "verify"
    subprocess.run(["git", "clone", "-q", str(origin), str(check)], check=True)
    assert 'imageTag: "v1.57.0"' in (check / "helm/genus-os/values-production.yaml").read_text()
    assert (check / "UNRELATED.md").exists()
