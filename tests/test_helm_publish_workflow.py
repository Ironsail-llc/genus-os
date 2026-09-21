"""Publishing the chart is a release step, so its shape is a contract too.

``helm install oci://ghcr.io/ironsail-llc/charts/genus-os`` is the documented
way to install Genus OS on Kubernetes. That line is a lie unless something
pushes the chart on every release, the pushed chart's version is the git tag,
and the pushed artifact is proved to render before anyone depends on it.

**The publish may not hang off a tag push.** semantic-release's release commit
is ``chore(release): X [skip ci]`` and the ``vX.Y.Z`` tag points at exactly that
commit, so GitHub skips the push event for that ref — a `push: tags: ['v*']`
trigger can never fire, and a test asserting the trigger exists would certify a
control that has never run. The publish therefore lives in
``release-and-build.yml``, keyed off the ``release`` job's outputs, which is the
same lever ``promote-production`` uses and demonstrably works.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-and-build.yml"
HELM_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "helm.yml"
CHART_README = REPO_ROOT / "helm" / "genus-os" / "README.md"
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "deployment.md"

#: Where the chart is published. A constant project registry, never a personal one.
OCI_REPOSITORY = "oci://ghcr.io/ironsail-llc/charts"

#: The job under test.
JOB = "publish-chart"

SHA = re.compile(r"^[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def release_workflow() -> dict[str, Any]:
    return yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def helm_workflow() -> dict[str, Any]:
    return yaml.safe_load(HELM_WORKFLOW.read_text(encoding="utf-8"))


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML resolves a bare `on:` key to the boolean True.
    return workflow.get("on", workflow.get(True))


def _run_script(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


# --------------------------------------------------------------------------
# where the publish lives, and why
# --------------------------------------------------------------------------


def test_the_publish_job_exists_in_the_release_pipeline(release_workflow) -> None:
    assert JOB in release_workflow["jobs"], (
        "the chart publish is not in the workflow that actually runs on a release"
    )


def test_no_workflow_publishes_the_chart_from_a_tag_push(release_workflow, helm_workflow) -> None:
    """The release commit carries `[skip ci]`, so its tag's push event is skipped."""
    for name, workflow in (
        ("release-and-build.yml", release_workflow),
        ("helm.yml", helm_workflow),
    ):
        push = _triggers(workflow).get("push") or {}
        assert "tags" not in push, (
            f"{name} triggers on a tag push; every tag in this repo is created by a "
            "`chore(release): … [skip ci]` commit, so that trigger can never fire"
        )


def test_the_publish_is_keyed_off_the_release_job(release_workflow) -> None:
    job = release_workflow["jobs"][JOB]
    assert "release" in job["needs"], "nothing tells the publish which version was released"
    condition = str(job["if"])
    assert "needs.release.outputs.new_release_published == 'true'" in condition
    assert "github.event_name == 'push'" in condition
    # The images the chart's appVersion names must exist before the chart that
    # points at them is published.
    assert "build" in job["needs"]
    assert "needs.build.result == 'success'" in condition


def test_the_publish_checks_out_the_released_tag(release_workflow) -> None:
    """`main` may already carry the next merge; the chart is the tag's."""
    checkout = next(
        step
        for step in release_workflow["jobs"][JOB]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout")
    )
    assert "new_release_version" in str(checkout["with"]["ref"])


def test_the_publish_asks_for_exactly_the_permission_it_needs(release_workflow) -> None:
    permissions = release_workflow["jobs"][JOB]["permissions"]
    assert permissions.get("packages") == "write"
    assert permissions.get("contents", "read") == "read"


def test_the_publish_refuses_a_chart_whose_version_is_not_the_release(release_workflow) -> None:
    """semantic-release bumps Chart.yaml; a mismatch means the release raced."""
    script = _run_script(release_workflow["jobs"][JOB])
    assert "new_release_version" in str(release_workflow["jobs"][JOB])
    assert "exit 1" in script, "the version assertion cannot fail the job"
    assert "Chart.yaml" in script or "helm show chart" in script


def test_the_publish_pushes_to_the_documented_repository(release_workflow) -> None:
    script = _run_script(release_workflow["jobs"][JOB])
    assert "helm package helm/genus-os" in script
    assert "helm push" in script
    assert OCI_REPOSITORY in script


def test_the_publish_proves_the_pushed_chart_renders(release_workflow) -> None:
    """A push nobody pulls back is a push that can be broken for a whole cycle."""
    script = _run_script(release_workflow["jobs"][JOB])
    assert "helm pull" in script
    assert "helm template" in script
    assert "values-production.yaml" in script


def test_the_registry_login_uses_the_workflow_token(release_workflow) -> None:
    job = release_workflow["jobs"][JOB]
    assert "helm registry login" in _run_script(job)
    assert "GITHUB_TOKEN" in str(job)


def test_every_action_in_the_publish_is_pinned_by_sha(release_workflow) -> None:
    for step in release_workflow["jobs"][JOB]["steps"]:
        uses = step.get("uses")
        if uses is None or uses.startswith("./"):
            continue
        _, separator, revision = uses.rpartition("@")
        assert separator and SHA.fullmatch(revision), f"{uses} is not SHA-pinned"


# --------------------------------------------------------------------------
# helm.yml stays what it was: the chart's own review gate
# --------------------------------------------------------------------------


def test_the_helm_workflow_only_lints(helm_workflow) -> None:
    assert JOB not in helm_workflow["jobs"]
    assert "publish" not in helm_workflow["jobs"]


def test_the_helm_workflow_keeps_its_path_filters(helm_workflow) -> None:
    """With no tag trigger there is no reason to lint every push to main."""
    triggers = _triggers(helm_workflow)
    assert "helm/**" in triggers["push"]["paths"]
    assert "helm/**" in triggers["pull_request"]["paths"]


# --------------------------------------------------------------------------
# what the docs promise
# --------------------------------------------------------------------------


def test_the_docs_install_from_the_registry_not_from_a_clone() -> None:
    for path in (CHART_README, DEPLOYMENT_DOC):
        body = path.read_text(encoding="utf-8")
        assert f"{OCI_REPOSITORY}/genus-os" in body, (
            f"{path.name} does not document the OCI install"
        )


def test_the_chart_readme_names_what_is_not_in_the_chart_yet() -> None:
    """`engine.plugins` renders no init container yet; saying so beats silence."""
    body = CHART_README.read_text(encoding="utf-8")
    assert "plugin" in body.lower()
