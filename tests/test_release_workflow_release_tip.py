"""Assertions that the release train releases main's tip or fails loudly.

The ``release`` job runs tens of minutes after its push, behind the release
gate. Bot commits (`[skip ci]`) can land on main in that window. Checked out
at the push SHA, semantic-release then sees a local branch behind the remote,
declines to publish, and exits 0: every downstream job skips and a merged
change is silently not released. These tests pin down that:

- the release job checks out ``main`` (its tip), not the push SHA;
- a guard refuses a tip that carries commits the gate did not verify (any
  commit after the gated SHA that is not a bot ``[skip ci]`` chore), or a
  gated SHA that is no longer an ancestor of the tip;
- the semantic-release step fails when semantic-release reports it is behind
  the remote, keeps its own exit code, and still derives
  ``new_release_published``/``new_release_version`` from the tag on HEAD;
- every job that builds, publishes or promotes the release works from the
  released version, never from ``github.sha``.

The two shell scripts are executed for real against throwaway git
repositories with a stub ``npx``, so the behaviour, not only the text, is
under test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-and-build.yml"
RELEASE_VERSION = "needs.release.outputs.new_release_version"
BOT = "github-actions[bot]"
BOT_EMAIL = "github-actions[bot]@users.noreply.github.com"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git"
)


@pytest.fixture(scope="module")
def jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _checkout(job: dict) -> dict:
    steps = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert len(steps) == 1, "expected exactly one checkout step"
    return steps[0].get("with", {})


def _step(job: dict, name: str) -> dict:
    matches = [s for s in job["steps"] if s.get("name") == name]
    assert len(matches) == 1, f"expected one step named {name!r}"
    return matches[0]


def _step_index(job: dict, name: str) -> int:
    return next(i for i, s in enumerate(job["steps"]) if s.get("name") == name)


# ── the release job checks out the tip ────────────────────────────────────


class TestReleaseCheckout:
    def test_checks_out_main_tip(self, jobs: dict) -> None:
        assert _checkout(jobs["release"]).get("ref") == "main"

    def test_keeps_full_history_and_push_key(self, jobs: dict) -> None:
        checkout = _checkout(jobs["release"])
        assert checkout.get("fetch-depth") == 0
        assert "secrets.CI_USER_PRIVATE_KEY" in checkout.get("ssh-key", "")


# ── the guard: the tip carries only what the gate verified ────────────────

GUARD = "Refuse a tip the gate did not verify"


def _git(repo: Path, *args: str, author: tuple[str, str] = ("Alice", "alice@example.com")) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": author[0],
        "GIT_AUTHOR_EMAIL": author[1],
        "GIT_COMMITTER_NAME": author[0],
        "GIT_COMMITTER_EMAIL": author[1],
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(
    repo: Path, message: str, author: tuple[str, str] = ("Alice", "alice@example.com")
) -> str:
    _git(repo, "commit", "--allow-empty", "-q", "-m", message, author=author)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    return path


def _run(script: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        cwd=cwd,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )


class TestTipGuard:
    def test_guard_runs_before_semantic_release(self, jobs: dict) -> None:
        release = jobs["release"]
        assert _step_index(release, GUARD) < _step_index(release, "Run semantic-release")

    def test_guard_is_given_the_gated_sha(self, jobs: dict) -> None:
        env = _step(jobs["release"], GUARD).get("env", {})
        assert env.get("GATED_SHA") == "${{ github.sha }}"

    def test_passes_when_tip_is_the_gated_sha(self, jobs: dict, repo: Path) -> None:
        gated = _commit(repo, "feat: a change")
        result = _run(_step(jobs["release"], GUARD)["run"], repo, {"GATED_SHA": gated})
        assert result.returncode == 0, result.stdout + result.stderr

    def test_passes_over_bot_skip_ci_chores(self, jobs: dict, repo: Path) -> None:
        gated = _commit(repo, "feat: a change")
        _commit(repo, "chore(staging): reset to v1.0.0 [skip ci]", author=(BOT, BOT_EMAIL))
        _commit(repo, "chore(deploy): promote v1.0.0 [skip ci]", author=(BOT, BOT_EMAIL))
        result = _run(_step(jobs["release"], GUARD)["run"], repo, {"GATED_SHA": gated})
        assert result.returncode == 0, result.stdout + result.stderr

    def test_fails_on_an_ungated_human_commit(self, jobs: dict, repo: Path) -> None:
        gated = _commit(repo, "feat: a change")
        _commit(repo, "fix: pushed while the gate ran")
        result = _run(_step(jobs["release"], GUARD)["run"], repo, {"GATED_SHA": gated})
        assert result.returncode != 0
        assert "::error::" in result.stdout

    def test_fails_on_a_human_commit_that_says_skip_ci(self, jobs: dict, repo: Path) -> None:
        gated = _commit(repo, "feat: a change")
        _commit(repo, "fix: sneaky [skip ci]")
        result = _run(_step(jobs["release"], GUARD)["run"], repo, {"GATED_SHA": gated})
        assert result.returncode != 0

    def test_fails_on_a_bot_commit_without_skip_ci(self, jobs: dict, repo: Path) -> None:
        gated = _commit(repo, "feat: a change")
        _commit(repo, "chore: bot change that ran CI", author=(BOT, BOT_EMAIL))
        result = _run(_step(jobs["release"], GUARD)["run"], repo, {"GATED_SHA": gated})
        assert result.returncode != 0

    def test_fails_when_gated_sha_is_not_an_ancestor(self, jobs: dict, repo: Path) -> None:
        _commit(repo, "feat: base")
        _git(repo, "checkout", "-q", "-b", "side")
        gated = _commit(repo, "feat: rewritten away")
        _git(repo, "checkout", "-q", "main")
        _commit(repo, "feat: the new history")
        result = _run(_step(jobs["release"], GUARD)["run"], repo, {"GATED_SHA": gated})
        assert result.returncode != 0
        assert "::error::" in result.stdout


# ── semantic-release: a skip is never green ───────────────────────────────


def _stub_npx(bin_dir: Path, output: str, code: int, tag: str | None = None) -> None:
    bin_dir.mkdir(exist_ok=True)
    tag_line = f"git tag {tag}\n" if tag else ""
    stub = bin_dir / "npx"
    stub.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' {output!r}\n{tag_line}exit {code}\n", encoding="utf-8"
    )
    stub.chmod(0o755)


def _run_semantic(
    jobs: dict, repo: Path, tmp_path: Path
) -> tuple[subprocess.CompletedProcess[str], str]:
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    script = _step(jobs["release"], "Run semantic-release")["run"]
    env = {
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(out),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_COMMITTER_NAME": "Alice",
        "GIT_COMMITTER_EMAIL": "alice@example.com",
    }
    result = _run(script, repo, env)
    return result, out.read_text(encoding="utf-8")


BEHIND = "[semantic-release] The local branch main is behind the remote one, therefore a new version won't be published."


class TestSemanticReleaseStep:
    def test_behind_the_remote_fails(self, jobs: dict, repo: Path, tmp_path: Path) -> None:
        _commit(repo, "feat: a change")
        _stub_npx(tmp_path / "bin", BEHIND, 0)
        result, _ = _run_semantic(jobs, repo, tmp_path)
        assert result.returncode != 0
        assert "::error::" in result.stdout
        assert "behind the remote" in result.stdout

    def test_semantic_release_failure_keeps_its_exit_code(
        self, jobs: dict, repo: Path, tmp_path: Path
    ) -> None:
        _commit(repo, "feat: a change")
        _stub_npx(tmp_path / "bin", "boom", 3)
        result, _ = _run_semantic(jobs, repo, tmp_path)
        assert result.returncode == 3

    def test_published_release_sets_outputs(self, jobs: dict, repo: Path, tmp_path: Path) -> None:
        _commit(repo, "feat: a change")
        _stub_npx(tmp_path / "bin", "Published release 1.2.3", 0, tag="v1.2.3")
        result, outputs = _run_semantic(jobs, repo, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "new_release_published=true" in outputs
        assert "new_release_version=1.2.3" in outputs

    def test_nothing_to_release_is_green_and_unpublished(
        self, jobs: dict, repo: Path, tmp_path: Path
    ) -> None:
        _commit(repo, "docs: a typo")
        _stub_npx(
            tmp_path / "bin", "There are no relevant changes, so no new version is released.", 0
        )
        result, outputs = _run_semantic(jobs, repo, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "new_release_published=false" in outputs


# ── downstream jobs work from the released version ────────────────────────


class TestDownstreamUsesTheRelease:
    @pytest.mark.parametrize("job", ["build-dist", "publish-chart"])
    def test_checks_out_the_release_tag(self, jobs: dict, job: str) -> None:
        assert _checkout(jobs[job]).get("ref") == f"v${{{{ {RELEASE_VERSION} }}}}"

    def test_image_build_checks_out_the_release_tag_on_push(self, jobs: dict) -> None:
        ref = _checkout(jobs["build"]).get("ref", "")
        assert ref.startswith(
            "${{ github.event_name == 'push' && needs.release.outputs.new_release_published == 'true'"
            f" && format('v{{0}}', {RELEASE_VERSION})"
        )

    @pytest.mark.parametrize("job", ["build-dist", "publish-chart", "promote-production"])
    def test_release_path_never_uses_the_push_sha(self, jobs: dict, job: str) -> None:
        assert "github.sha" not in yaml.safe_dump(jobs[job])

    def test_promotion_promotes_the_released_version(self, jobs: dict) -> None:
        # It commits a GitOps bump, so it checks out main to push onto; what
        # it promotes is the version, never the checked-out tree.
        assert _checkout(jobs["promote-production"]).get("ref") == "main"
        promote = _step(jobs["promote-production"], "Promote release in GitOps values")
        assert promote["env"]["VERSION"] == f"${{{{ {RELEASE_VERSION} }}}}"
