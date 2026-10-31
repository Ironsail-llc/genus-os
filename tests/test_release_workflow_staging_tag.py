"""The staging image tag the build pushes is the tag the staging bump writes.

On a ``pull_request`` event ``github.sha`` is the MERGE commit GitHub made,
not the head the contributor pushed. The build job checks out the PR head,
but ``docker/metadata-action``'s ``{{sha}}`` still meant the merge commit, so
the image went out as ``pr-N-sha-<merge>`` while ``deploy-staging`` wrote
``pr-N-sha-<head>`` into values-staging.yaml: staging pulled a tag that did
not exist. Every place that names the PR image must use the head commit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-and-build.yml"


@pytest.fixture(scope="module")
def jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _step(job: dict, *, uses: str | None = None, name: str | None = None) -> dict:
    for step in job["steps"]:
        if uses and str(step.get("uses", "")).startswith(uses):
            return step
        if name and step.get("name") == name:
            return step
    raise AssertionError(f"no step uses={uses!r} name={name!r}")


def test_the_image_metadata_tags_the_pr_head_not_the_merge_commit(jobs: dict) -> None:
    meta = _step(jobs["build"], uses="docker/metadata-action")
    assert str(meta.get("env", {}).get("DOCKER_METADATA_PR_HEAD_SHA")).lower() == "true"
    assert "pr-${{ github.event.pull_request.number }}-sha-{{sha}}" in meta["with"]["tags"]


def test_the_staging_bump_writes_the_pr_head(jobs: dict) -> None:
    bump = _step(jobs["deploy-staging"], name="Bump values-staging.yaml")
    assert bump["env"]["SHORT_SHA"] == "${{ github.event.pull_request.head.sha }}"


def test_the_staging_comment_names_the_pr_head(jobs: dict) -> None:
    owners = [
        job
        for job in jobs.values()
        if any(step.get("name") == "Compute short SHA" for step in job.get("steps", []))
    ]
    assert owners, "no job computes the short SHA for the staging comment"
    for job in owners:
        sha = _step(job, name="Compute short SHA")
        assert sha.get("env", {}).get("HEAD_SHA") == "${{ github.event.pull_request.head.sha }}"
        assert "HEAD_SHA" in sha["run"] and "GITHUB_SHA" not in sha["run"]
