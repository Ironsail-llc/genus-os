"""Publishing the chart is a release step, so its shape is a contract too.

``helm install oci://ghcr.io/ironsail-llc/charts/genus-os`` is the documented
way to install Genus OS on Kubernetes. That line is a lie unless something
pushes the chart on every release, the pushed chart's version is the git tag,
and the pushed artifact is proved to render before anyone depends on it.

So: the workflow must fire on a ``v*`` tag without a path filter that could
quietly skip a release, refuse to publish a chart whose version is not the tag,
push to the documented repository, and pull the result back and template it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "helm.yml"
CHART_PATH = REPO_ROOT / "helm" / "genus-os" / "Chart.yaml"
CHART_README = REPO_ROOT / "helm" / "genus-os" / "README.md"
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "deployment.md"

#: Where the chart is published. A constant project registry, never a personal one.
OCI_REPOSITORY = "oci://ghcr.io/ironsail-llc/charts"

SHA = re.compile(r"^[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML resolves a bare `on:` key to the boolean True.
    return workflow.get("on", workflow.get(True))


def _run_script(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def test_a_release_tag_triggers_the_workflow(workflow: dict[str, Any]) -> None:
    push = _triggers(workflow)["push"]
    assert "v*" in push["tags"], "a release tag does not run this workflow"
    assert "paths" not in push, (
        "a path filter on the push trigger means a release whose commit happens "
        "not to touch helm/ publishes no chart, silently"
    )


def test_publish_runs_only_on_a_tag_and_only_after_the_chart_is_linted(
    workflow: dict[str, Any],
) -> None:
    publish = workflow["jobs"]["publish"]
    assert publish["needs"] == "lint-and-test" or "lint-and-test" in publish["needs"]
    assert "refs/tags/v" in str(publish["if"]), "publish is not restricted to release tags"


def test_publish_asks_for_exactly_the_permission_it_needs(workflow: dict[str, Any]) -> None:
    permissions = workflow["jobs"]["publish"]["permissions"]
    assert permissions.get("packages") == "write"
    assert permissions.get("contents", "read") == "read"


def test_publish_refuses_a_chart_whose_version_is_not_the_tag(workflow: dict[str, Any]) -> None:
    """semantic-release bumps Chart.yaml; a mismatch means the release raced."""
    script = _run_script(workflow["jobs"]["publish"])
    assert "GITHUB_REF_NAME" in script
    assert "exit 1" in script, "the version assertion cannot fail the job"
    assert "Chart.yaml" in script or "helm show chart" in script


def test_publish_pushes_to_the_documented_repository(workflow: dict[str, Any]) -> None:
    script = _run_script(workflow["jobs"]["publish"])
    assert "helm package helm/genus-os" in script
    assert "helm push" in script
    assert OCI_REPOSITORY in script


def test_publish_proves_the_pushed_chart_renders(workflow: dict[str, Any]) -> None:
    """A push nobody pulls back is a push that can be broken for a whole cycle."""
    script = _run_script(workflow["jobs"]["publish"])
    assert "helm pull" in script
    assert "helm template" in script
    assert "values-production.yaml" in script


def test_every_action_in_publish_is_pinned_by_sha(workflow: dict[str, Any]) -> None:
    for step in workflow["jobs"]["publish"]["steps"]:
        uses = step.get("uses")
        if uses is None or uses.startswith("./"):
            continue
        _, separator, revision = uses.rpartition("@")
        assert separator and SHA.fullmatch(revision), f"{uses} is not SHA-pinned"


def test_the_registry_login_uses_the_workflow_token(workflow: dict[str, Any]) -> None:
    script = _run_script(workflow["jobs"]["publish"])
    assert "helm registry login" in script
    assert "GITHUB_TOKEN" in str(workflow["jobs"]["publish"])


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
