"""Assertions on the PyPI-publish and release-asset jobs in release-and-build.yml.

There is no way to execute a release workflow pre-merge, so these tests pin
down the properties that matter statically:

- ``build-dist`` runs on every release (ungated by PYPI_PUBLISH_ENABLED),
  builds from the released tag on a clean checkout, and attaches the
  distributions to the GitHub Release.
- ``publish-pypi`` is double-gated (release published AND the repository
  variable PYPI_PUBLISH_ENABLED == 'true') at the *job* level so it shows
  as skipped -- never red -- while the variable is unset.
- Trusted Publishing: ``id-token: write`` is scoped to the publish job only,
  never workflow-wide, and the publish action is pinned to a full commit SHA
  (the repo-wide pin gate is scripts/check_workflow_action_pins.py; this
  re-asserts it for the publish action specifically).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-and-build.yml"
SHA_PIN = re.compile(r"@[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def jobs(workflow: dict) -> dict:
    return workflow["jobs"]


def test_workflow_permissions_do_not_grant_id_token(workflow: dict) -> None:
    """OIDC must be a job-level opt-in, never workflow-wide."""
    assert "id-token" not in (workflow.get("permissions") or {})


class TestBuildDist:
    def test_job_exists_and_depends_on_release(self, jobs: dict) -> None:
        job = jobs["build-dist"]
        assert "release" in job["needs"]

    def test_runs_on_every_release_regardless_of_pypi_flag(self, jobs: dict) -> None:
        condition = jobs["build-dist"]["if"]
        assert "new_release_published == 'true'" in condition
        assert "PYPI_PUBLISH_ENABLED" not in condition

    def test_checks_out_the_released_tag(self, jobs: dict) -> None:
        checkout = next(
            step
            for step in jobs["build-dist"]["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        assert checkout["with"]["ref"] == "v${{ needs.release.outputs.new_release_version }}"

    def test_attaches_distributions_to_the_github_release(self, jobs: dict) -> None:
        scripts = [step.get("run", "") for step in jobs["build-dist"]["steps"]]
        assert any("gh release upload" in script for script in scripts)

    def test_guards_against_instance_file_leaks_in_wheel_and_sdist(self, jobs: dict) -> None:
        scripts = " ".join(step.get("run", "") for step in jobs["build-dist"]["steps"])
        assert "zipfile -l dist/*.whl" in scripts
        assert "tar -tzf dist/*.tar.gz" in scripts
        assert "delphi" in scripts


class TestPublishPypi:
    def test_job_gated_on_release_and_repo_variable(self, jobs: dict) -> None:
        condition = jobs["publish-pypi"]["if"]
        assert "needs.release.outputs.new_release_published == 'true'" in condition
        assert "vars.PYPI_PUBLISH_ENABLED == 'true'" in condition
        # No always()/failure() escape hatch: with a plain condition GitHub
        # marks the job "skipped" when it is false -- the green-not-red
        # semantics the gate relies on.
        assert "always()" not in condition

    def test_publishes_the_artifact_built_from_the_clean_tag_checkout(self, jobs: dict) -> None:
        job = jobs["publish-pypi"]
        assert "build-dist" in job["needs"]
        uses = [str(step.get("uses", "")) for step in job["steps"]]
        assert any(u.startswith("actions/download-artifact@") for u in uses)
        # The publish job must not rebuild: no run steps invoking the build.
        assert not any("python -m build" in step.get("run", "") for step in job["steps"])

    def test_uses_trusted_publishing_environment_and_job_scoped_oidc(self, jobs: dict) -> None:
        job = jobs["publish-pypi"]
        assert job["environment"] == "pypi"
        assert job["permissions"]["id-token"] == "write"
        # No password/token secret anywhere in the job.
        assert "PYPI_API_TOKEN" not in yaml.safe_dump(job)

    def test_publish_action_is_sha_pinned(self, jobs: dict) -> None:
        publish = next(
            str(step["uses"])
            for step in jobs["publish-pypi"]["steps"]
            if str(step.get("uses", "")).startswith("pypa/gh-action-pypi-publish@")
        )
        assert SHA_PIN.search(publish), f"not pinned to a full commit SHA: {publish}"
