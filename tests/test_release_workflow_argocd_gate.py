"""Assertions on the ArgoCD deploy kill switch in release-and-build.yml.

The ArgoCD automation token (repo secret ``ARGOCD_TOKEN``) expired and every
production release failed the ``deploy-production`` job red, training alarm
fatigue on an otherwise-green release train. ``deploy-production`` and
``deploy-staging`` (which shares the same token) are gated behind the repo
variable ``ARGOCD_DEPLOY_ENABLED`` so they show SKIPPED (grey) rather than
FAILED (red) while the token is dead. These tests pin down statically that:

- both jobs carry the gate, ANDed with (not replacing) their existing
  conditions;
- no other job depends on either via ``needs:``, so skipping them cannot
  cascade a skip into ``promote-production`` or any other job.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-and-build.yml"
GATE = "vars.ARGOCD_DEPLOY_ENABLED == 'true'"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def jobs(workflow: dict) -> dict:
    return workflow["jobs"]


class TestDeployProductionGate:
    def test_gated_on_repo_variable(self, jobs: dict) -> None:
        condition = jobs["deploy-production"]["if"]
        assert GATE in condition

    def test_existing_conditions_preserved(self, jobs: dict) -> None:
        condition = jobs["deploy-production"]["if"]
        assert "needs.build.result == 'success'" in condition
        assert "needs.promote-production.result == 'success'" in condition
        assert "needs.release.outputs.new_release_published == 'true'" in condition
        assert "github.event_name == 'push'" in condition
        assert "github.ref == 'refs/heads/main'" in condition


class TestDeployStagingGate:
    def test_gated_on_repo_variable(self, jobs: dict) -> None:
        condition = jobs["deploy-staging"]["if"]
        assert GATE in condition

    def test_existing_conditions_preserved(self, jobs: dict) -> None:
        condition = jobs["deploy-staging"]["if"]
        assert "needs.build.result == 'success'" in condition
        assert "github.event_name == 'pull_request'" in condition
        assert "contains(github.event.pull_request.labels.*.name, 'deploy-staging')" in condition


def test_no_job_depends_on_the_gated_jobs(jobs: dict) -> None:
    """Skipping deploy-production/deploy-staging must not cascade.

    promote-production already runs (and commits the GitOps promotion)
    independently of deploy-production, and no job lists either gated job
    in its `needs:` -- so grey-skipping them changes nothing downstream.
    """
    for name, job in jobs.items():
        needs = job.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        assert "deploy-production" not in needs, name
        assert "deploy-staging" not in needs, name


def test_reenable_instructions_reference_the_token_secret_by_name() -> None:
    """The re-enable path must name the actual expired secret, not a guess."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "ARGOCD_TOKEN" in text
    assert "ARGOCD_DEPLOY_ENABLED=true" in text
