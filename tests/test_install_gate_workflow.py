"""The install gate's own shape is a contract, because CI is the only place it runs.

A gate that quietly stops asserting is worse than no gate — it keeps reporting
green while the front door rots. Every assertion here pins one thing the
workflow must still be doing: that it replays the documented blocks rather than
a copy of them, that it makes the four readiness calls, that it asks the doctor,
that it gets a real completion out of an agent, and that it walks the setup
wizard from a fresh instance to a claimed one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "install-gate.yml"

#: Paths whose change must re-run the gate on the pull request that changes them.
WATCHED_PATHS = frozenset(
    {
        "robothor/init/**",
        "robothor/setup*.py",
        "robothor/settings/**",
        "robothor/secrets/**",
        "robothor/doctor/**",
        "infra/docker-compose*.yml",
        "Dockerfile.*",
        "docs/quickstart.md",
        "scripts/load-secrets.sh",
        "crm/bridge/routers/setup.py",
        "tests/acceptance/**",
        ".github/workflows/install-gate.yml",
    }
)

SHA = re.compile(r"^[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    assert WORKFLOW_PATH.is_file(), f"{WORKFLOW_PATH} is missing"
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def raw() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _run_script(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML resolves a bare `on:` key to the boolean True.
    return workflow.get("on", workflow.get(True))


def test_the_workflow_parses(workflow: dict[str, Any]) -> None:
    assert isinstance(workflow, dict)
    assert workflow["name"]


def test_both_substrates_have_a_job(workflow: dict[str, Any]) -> None:
    assert set(workflow["jobs"]) >= {"compose", "local"}


def test_it_runs_nightly_on_dispatch_and_on_the_paths_that_break_installs(
    workflow: dict[str, Any],
) -> None:
    triggers = _triggers(workflow)

    assert triggers["schedule"] == [{"cron": "17 6 * * *"}]
    assert "workflow_dispatch" in triggers
    assert set(triggers["pull_request"]["paths"]) >= WATCHED_PATHS


@pytest.mark.parametrize("job_name", ["compose", "local"])
def test_every_job_is_time_boxed(workflow: dict[str, Any], job_name: str) -> None:
    """An install that hangs must fail, not occupy a runner for six hours."""
    assert workflow["jobs"][job_name]["timeout-minutes"] == 25


@pytest.mark.parametrize("job_name", ["compose", "local"])
def test_every_action_is_pinned_by_sha(workflow: dict[str, Any], job_name: str) -> None:
    for step in workflow["jobs"][job_name]["steps"]:
        uses = step.get("uses")
        if uses is None or uses.startswith("./"):
            continue
        _, separator, revision = uses.rpartition("@")
        assert separator and SHA.fullmatch(revision), f"{job_name}: {uses} is not SHA-pinned"


@pytest.mark.parametrize(
    ("job_name", "block"),
    [("compose", "compose"), ("local", "local")],
)
def test_each_job_replays_the_documented_block(
    workflow: dict[str, Any], job_name: str, block: str
) -> None:
    """The gate's whole point: the quickstart is the test, not a copy of it."""
    script = _run_script(workflow["jobs"][job_name])

    assert "scripts/extract_doc_commands.py" in script
    assert f"--block {block}" in script
    assert "docs/quickstart.md" in script


@pytest.mark.parametrize("job_name", ["compose", "local"])
def test_each_job_asserts_the_doctor_is_green(workflow: dict[str, Any], job_name: str) -> None:
    script = _run_script(workflow["jobs"][job_name])

    assert "genus doctor --json" in script


@pytest.mark.parametrize("job_name", ["compose", "local"])
def test_each_job_gets_a_real_completion_out_of_an_agent(
    workflow: dict[str, Any], job_name: str
) -> None:
    """`genus doctor` green with no model reachable is the exact defect this catches."""
    script = _run_script(workflow["jobs"][job_name])

    assert "say pong" in script
    assert "--agent main" in script


@pytest.mark.parametrize("job_name", ["compose", "local"])
def test_each_job_checks_the_fleet_resolves_to_the_chosen_model_first(
    workflow: dict[str, Any], job_name: str
) -> None:
    """A fleet pinned to a model the wizard never chose fails as four
    consecutive provider timeouts, and that log names everything but the
    cause. The assertion has to come before the completion call."""
    steps = workflow["jobs"][job_name]["steps"]
    names = [str(step.get("name", "")) for step in steps]
    resolves = next(i for i, name in enumerate(names) if "resolves to the model" in name)
    completes = next(i for i, name in enumerate(names) if "real completion" in name)

    assert resolves < completes
    assert "load_agent_config" in str(steps[resolves]["run"])


@pytest.mark.parametrize("job_name", ["compose", "local"])
def test_each_job_walks_the_setup_wizard_to_completion(
    workflow: dict[str, Any], job_name: str
) -> None:
    """A fresh instance must be claimable, and then closed for good."""
    script = _run_script(workflow["jobs"][job_name])

    assert "/api/setup/status" in script
    assert "/api/setup/claim" in script
    assert "/api/setup/operator" in script


def test_the_compose_job_asserts_every_ready_endpoint(workflow: dict[str, Any]) -> None:
    script = _run_script(workflow["jobs"]["compose"])

    assert "curl -f" in script
    for service in ("engine", "bridge", "orchestrator", "dashboard"):
        assert service in script, f"no readiness assertion names the {service}"


def test_the_compose_job_uploads_logs_when_it_fails(workflow: dict[str, Any]) -> None:
    """The first thing anyone needs at 06:17 UTC is the container logs."""
    uploads = [
        step
        for step in workflow["jobs"]["compose"]["steps"]
        if "upload-artifact" in str(step.get("uses", ""))
    ]

    assert uploads, "no log artifact is uploaded"
    assert any("failure()" in str(step.get("if", "")) for step in uploads)


def test_the_gate_needs_no_secrets(raw: str) -> None:
    """A gate that needed a provider key could not run on a fork, or nightly."""
    assert not re.findall(r"\$\{\{\s*secrets\.", raw)


def test_fixtures_use_the_example_domain_only(raw: str) -> None:
    for address in re.findall(r"[\w.+-]+@[\w.-]+\.\w+", raw):
        assert address.endswith("@example.com"), f"{address} is not an example.com fixture"
