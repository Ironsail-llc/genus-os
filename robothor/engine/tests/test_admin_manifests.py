"""The engine's manifest store: the files only this engine's pod can reach.

A child tenant's engine runs in its own deployment with its own workspace. The
bridge cannot reach that filesystem, so the agent builder's reads and writes
for a child tenant go through these routes on the child's engine. The bridge
keeps the merge, validation, history and audit logic; this module only reads
and writes files, atomically, inside the engine's own manifest directory and
workspace — the same primitives the bridge uses for the root tenant
(``robothor.engine.manifest_store``).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import yaml
from fastapi import APIRouter
from starlette.testclient import TestClient

AGENT = {
    "id": "demo-agent",
    "name": "Demo Agent",
    "description": "Demo",
    "instruction_file": "brain/DEMO_AGENT.md",
}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    (tmp_path / "docs" / "agents").mkdir(parents=True)
    (tmp_path / "brain").mkdir()
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("ROBOTHOR_MANIFEST_DIR", raising=False)
    return tmp_path


@pytest.fixture
def client(workspace):
    mock_config = MagicMock()
    mock_config.tenant_id = "test-tenant"
    mock_config.bot_token = ""
    mock_config.port = 18800

    from robothor.engine.health import create_health_app

    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_completion_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.db.connection.get_connection"),
    ):
        app = create_health_app(mock_config, runner=None, workflow_engine=None)
    return TestClient(app, raise_server_exceptions=False)


def _seed(workspace, agent=AGENT):
    (workspace / "docs" / "agents" / f"{agent['id']}.yaml").write_text(yaml.safe_dump(agent))
    (workspace / "brain" / "DEMO_AGENT.md").write_text("# Demo\n")


def test_the_scan_lists_manifests_and_names_failures(client, workspace):
    _seed(workspace)
    (workspace / "docs" / "agents" / "broken.yaml").write_text("id: [unclosed\n")
    body = client.get("/api/admin/manifests").json()
    assert [m["id"] for m in body["manifests"]] == ["demo-agent"]
    assert [f["filename"] for f in body["failures"]] == ["broken.yaml"]
    assert "main_agent_id" in body
    assert "default_model" in body
    # Never a filesystem path in a response.
    assert str(workspace) not in str(body)


def test_one_manifest_with_its_instructions(client, workspace):
    _seed(workspace)
    body = client.get("/api/admin/manifests/demo-agent").json()
    assert yaml.safe_load(body["yaml"])["id"] == "demo-agent"
    assert body["instructions"] == "# Demo\n"


def test_an_absent_manifest_is_a_404(client, workspace):
    assert client.get("/api/admin/manifests/nobody").status_code == 404


def test_a_create_writes_both_files_and_refuses_a_second(client, workspace):
    payload = {
        "yaml": yaml.safe_dump(AGENT),
        "create": True,
        "instruction_file": "brain/DEMO_AGENT.md",
        "instructions": "# Demo\n",
    }
    assert client.put("/api/admin/manifests/demo-agent", json=payload).status_code == 200
    assert (workspace / "docs" / "agents" / "demo-agent.yaml").is_file()
    assert (workspace / "brain" / "DEMO_AGENT.md").read_text() == "# Demo\n"
    assert client.put("/api/admin/manifests/demo-agent", json=payload).status_code == 409


def test_an_update_snapshots_the_previous_version(client, workspace):
    _seed(workspace)
    edited = {**AGENT, "name": "Renamed"}
    response = client.put(
        "/api/admin/manifests/demo-agent", json={"yaml": yaml.safe_dump(edited), "create": False}
    )
    assert response.status_code == 200, response.text
    on_disk = yaml.safe_load((workspace / "docs" / "agents" / "demo-agent.yaml").read_text())
    assert on_disk["name"] == "Renamed"
    history = list((workspace / "docs" / "agents" / ".history" / "demo-agent").glob("*.yaml"))
    assert len(history) == 1


def test_an_update_of_an_absent_manifest_is_a_404(client, workspace):
    response = client.put(
        "/api/admin/manifests/nobody", json={"yaml": "id: nobody\n", "create": False}
    )
    assert response.status_code == 404


def test_an_instruction_file_outside_brain_is_refused(client, workspace):
    response = client.put(
        "/api/admin/manifests/demo-agent",
        json={
            "yaml": yaml.safe_dump(AGENT),
            "create": True,
            "instruction_file": "../outside.md",
            "instructions": "x",
        },
    )
    assert response.status_code == 422
    assert not (workspace.parent / "outside.md").exists()
    # The refusal says what kind of thing was refused, in the route's own
    # words; the validator's exception text is not echoed to the browser.
    body = response.json()
    assert body["error"]
    assert "Invalid" not in body["error"]
    assert "instruction_file" not in body["error"]


def test_a_path_shaped_agent_id_is_refused(client, workspace):
    response = client.put(
        "/api/admin/manifests/..%2Fescape", json={"yaml": "id: x\n", "create": True}
    )
    assert response.status_code in (404, 422)


def test_retire_moves_the_manifest_aside(client, workspace):
    _seed(workspace)
    assert client.post("/api/admin/manifests/demo-agent/retire").status_code == 200
    assert not (workspace / "docs" / "agents" / "demo-agent.yaml").exists()
    assert (workspace / "docs" / "agents" / "retired" / "demo-agent.yaml").is_file()
    assert client.post("/api/admin/manifests/demo-agent/retire").status_code == 404


def test_the_validation_context_reports_what_exists(client, workspace):
    _seed(workspace)
    (workspace / "docs" / "agents" / "_defaults.yaml").write_text("model:\n  primary: m-1\n")
    body = client.post(
        "/api/admin/manifests/context",
        json={"paths": ["brain/DEMO_AGENT.md", "brain/MISSING.md", "../etc/passwd"]},
    ).json()
    assert body["existing"] == ["brain/DEMO_AGENT.md"]
    assert yaml.safe_load(body["defaults_yaml"]) == {"model": {"primary": "m-1"}}
    assert "schema_yaml" in body
