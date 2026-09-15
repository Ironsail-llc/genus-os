"""Exporting an installed agent over HTTP — the Helm's Share button's two routes.

Handing an agent to somebody else is an operator act with a blast radius that
is not obvious from the verb: the bytes leaving are the agent's instructions,
and an instruction file is exactly where an operator pastes a key "just for a
minute". So:

* operator only, audited with the agent id and nothing else
* the same secret gate the CLI export runs — a hit is a 409 naming the file,
  and never a partial download
* adapters are NEVER carried over HTTP: an adapter names a command to run, and
  a download is the wrong place to make that decision
* the plan route says what WOULD be exported, so the Helm can show it first
"""

from __future__ import annotations

import io
import tarfile
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from robothor.auth import tokens

MANIFEST = """\
id: note-taker
name: Note Taker
description: Takes notes
version: "1.0.0"
department: custom

model:
  primary: openrouter/xiaomi/mimo-v2-pro

schedule:
  cron: "0 * * * *"
  timezone: UTC

delivery:
  mode: none

tools_allowed: []
instruction_file: brain/agents/note-taker.md
"""

SCHEMA = {
    "required": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "version": {"type": "string"},
        "department": {"type": "string", "enum": ["custom"]},
    }
}


@pytest.fixture(autouse=True)
def auth_key(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    (root / "docs" / "agents").mkdir(parents=True)
    (root / "brain" / "agents").mkdir(parents=True)
    (root / "templates" / "agents").mkdir(parents=True)
    (root / "docs" / "agents" / "schema.yaml").write_text(yaml.dump(SCHEMA))
    (root / "templates" / "agents" / "_defaults.yaml").write_text(yaml.dump({}))
    (root / "docs" / "agents" / "note-taker.yaml").write_text(MANIFEST)
    (root / "brain" / "agents" / "note-taker.md").write_text("# Note Taker\n")
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(root))
    from robothor.settings import reset_settings

    reset_settings()
    yield root
    reset_settings()


def _client(role: str, *, user_id: str = "human-1") -> TestClient:
    from bridge_service import app
    from routers._operator import PLATFORM_TENANT

    token = tokens.issue_access_token(user_id, PLATFORM_TENANT, role)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture
def audit():
    with patch("routers._audit.log_event") as log_event:
        yield log_event


class TestGate:
    @pytest.mark.parametrize("role", ["member", "user", "viewer", "auditor"])
    def test_a_non_operator_cannot_export(self, workspace, audit, role):
        response = _client(role).post("/api/installed-agents/note-taker/export")
        assert response.status_code == 403

    @pytest.mark.parametrize("role", ["member", "viewer"])
    def test_a_non_operator_cannot_read_the_plan(self, workspace, audit, role):
        response = _client(role).get("/api/installed-agents/note-taker/export/plan")
        assert response.status_code == 403

    def test_an_unauthenticated_caller_is_refused_exactly_as_install_is(self, workspace, audit):
        """Pinned against the sibling route rather than against a number.

        Which refusal an anonymous caller gets on this prefix is the
        middleware's decision, not this route's. Asserting the literal code
        would make this test fail the day that policy changes for good reasons;
        asserting that export answers whatever install answers is the property
        that actually matters — a new route must not be the loose one.
        """
        from bridge_service import app

        anonymous = TestClient(app)
        install = anonymous.post(
            "/api/installed-agents/install", json={"slug": "note-taker", "variables": {}}
        )
        export = anonymous.post("/api/installed-agents/note-taker/export")
        plan = anonymous.get("/api/installed-agents/note-taker/export/plan")

        assert install.status_code in (401, 403)
        assert export.status_code == install.status_code
        assert plan.status_code == install.status_code


class TestExport:
    def test_an_operator_downloads_a_bundle_and_the_act_is_audited(self, workspace, audit):
        response = _client("owner").post("/api/installed-agents/note-taker/export")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/gzip")
        assert "agent-note-taker-1.0.0.tar.gz" in response.headers["content-disposition"]

        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
            names = archive.getnames()
        assert "note-taker/bundle.yaml" in names

        assert audit.call_args.args[0] == "helm.agent.export"
        assert audit.call_args.kwargs["action"] == "note-taker"

    def test_the_audit_row_carries_the_id_and_nothing_else(self, workspace, audit):
        _client("owner").post("/api/installed-agents/note-taker/export")
        payload = str(audit.call_args)
        assert "note-taker" in payload
        assert "brain/" not in payload

    def test_the_bytes_are_the_same_bundle_the_cli_writes(self, workspace, audit, tmp_path):
        from robothor.templates.bundle import read_bundle
        from robothor.templates.exporter import export_agent

        response = _client("owner").post("/api/installed-agents/note-taker/export")
        assert response.status_code == 200

        out = tmp_path / "cli"
        cli = export_agent("note-taker", out=out, repo_root=workspace)

        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
            extracted = tmp_path / "http"
            archive.extractall(extracted, filter="data")  # noqa: S202 - our own bytes
        served = read_bundle(extracted / "note-taker")

        assert served.files == cli.manifest.files

    def test_a_credential_literal_is_a_409_naming_the_file(self, workspace, audit):
        (workspace / "brain" / "agents" / "note-taker.md").write_text(
            "# Note Taker\n\nAPI_KEY=sk-or-v1-abcdefghijklmnop\n"
        )

        response = _client("owner").post("/api/installed-agents/note-taker/export")

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "instructions.template.md" in detail
        assert "sk-or-v1-abcdefghijklmnop" not in detail

    def test_an_unknown_agent_is_a_404(self, workspace, audit):
        assert _client("owner").post("/api/installed-agents/nobody/export").status_code == 404

    def test_an_unsafe_id_is_a_400(self, workspace, audit):
        response = _client("owner").post("/api/installed-agents/..%2Fetc/export")
        assert response.status_code in (400, 404)


class TestPlan:
    def test_the_plan_describes_what_would_be_exported(self, workspace, audit):
        response = _client("owner").get("/api/installed-agents/note-taker/export/plan")

        assert response.status_code == 200
        body = response.json()
        assert body["agent_id"] == "note-taker"
        assert body["version"] == "1.0.0"
        assert body["kind"] == "agent-bundle"
        assert body["filename"] == "agent-note-taker-1.0.0.tar.gz"
        assert any(f["path"] == "setup.yaml" for f in body["files"])
        assert set(body["requires"]) == {"plugins", "adapters", "secrets", "skills"}
        assert body["include_adapters"] is False

    def test_the_plan_refuses_a_credential_the_same_way(self, workspace, audit):
        (workspace / "brain" / "agents" / "note-taker.md").write_text(
            "# Note Taker\n\nAPI_KEY=sk-or-v1-abcdefghijklmnop\n"
        )
        response = _client("owner").get("/api/installed-agents/note-taker/export/plan")
        assert response.status_code == 409
        assert "sk-or-v1-abcdefghijklmnop" not in response.text
