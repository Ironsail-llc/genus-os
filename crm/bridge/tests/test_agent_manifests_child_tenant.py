"""The agent builder, fleet and automations for a CHILD tenant.

A child tenant's manifests live on its own engine's volume, which the bridge
cannot reach. So for any acting tenant other than the platform tenant, every
manifest read and write goes through that tenant's engine
(``/api/admin/manifests*``), and the bridge's own workspace — the ROOT fleet —
is never read or written. Showing a child admin the root fleet would be a leak;
writing a child's agent into it would hand the root engine an agent nobody in
the root tenant made.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEY = "test-signing-key-at-least-32-bytes-long-xyz"
ROOT = "acme-parent"
CHILD = "acme-child-a"

CHILD_AGENT = {
    "id": "child-agent",
    "name": "Child Agent",
    "description": "Belongs to the child",
    "version": "2026-09-01",
    "department": "operations",
    "schedule": {"cron": "0 9 * * *", "timezone": "UTC"},
    "delivery": {"mode": "none"},
    "tools_allowed": ["read_file"],
    "instruction_file": "brain/CHILD_AGENT.md",
}

CHILD_MODEL = "openrouter/example/child-model"

ROOT_AGENT = {**CHILD_AGENT, "id": "root-agent", "name": "Root Agent"}


class ChildEngine:
    """The child tenant's engine, holding its own manifests in memory."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {"child-agent": yaml.safe_dump(CHILD_AGENT)}
        self.instructions: dict[str, str] = {"brain/CHILD_AGENT.md": "# Child Agent\n"}
        self.retired: list[str] = []
        self.calls: list[tuple[str, str, str | None]] = []

    async def __call__(self, method, path, *, json=None, timeout=30, tenant=None):
        self.calls.append((method, path, tenant))
        assert tenant == CHILD, f"{method} {path} reached the engine of {tenant!r}"
        if path == "/api/admin/tools":
            return 200, {"tools": ["read_file", "exec"], "count": 2}
        if path == "/api/admin/scheduler/reconcile":
            return 200, {"added": [], "replaced": [], "pruned": [], "blocked": {}, "clean": True}
        if path == "/api/admin/manifests" and method == "GET":
            return 200, {
                "manifests": [yaml.safe_load(text) for text in self.files.values()],
                "failures": [],
                "main_agent_id": "child-agent",
                "default_model": CHILD_MODEL,
            }
        if path == "/api/admin/manifests/context":
            return 200, {
                "defaults_yaml": f"model:\n  primary: {CHILD_MODEL}\n",
                "schema_yaml": "",
                "existing": [p for p in (json or {}).get("paths", []) if p in self.instructions],
            }
        if path.endswith("/retire"):
            agent_id = path.split("/")[-2]
            if agent_id not in self.files:
                return 404, {"error": "no such agent"}
            self.retired.append(agent_id)
            del self.files[agent_id]
            return 200, {"retired": True}
        if path.startswith("/api/admin/manifests/"):
            agent_id = path.rsplit("/", 1)[-1]
            if method == "GET":
                if agent_id not in self.files:
                    return 404, {"error": "no such agent"}
                document = yaml.safe_load(self.files[agent_id])
                return 200, {
                    "yaml": self.files[agent_id],
                    "instructions": self.instructions.get(document.get("instruction_file"), ""),
                }
            if method == "PUT":
                body: dict[str, Any] = json or {}
                if body.get("create") and agent_id in self.files:
                    return 409, {"error": "an agent with that id already exists"}
                if not body.get("create") and agent_id not in self.files:
                    return 404, {"error": "no such agent"}
                if body.get("instruction_file"):
                    self.instructions.setdefault(body["instruction_file"], body["instructions"])
                self.files[agent_id] = body["yaml"]
                return 200, {"written": True}
        if path.endswith("/trigger"):
            return 200, {"status": "triggered"}
        raise AssertionError(f"unexpected engine call: {method} {path}")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    import routers._operator as operator

    from robothor.auth import tokens

    monkeypatch.setattr(operator, "PLATFORM_TENANT", ROOT)
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


@pytest.fixture
def root_workspace(tmp_path, monkeypatch):
    """The bridge host's own workspace — the ROOT fleet. Must stay untouched."""
    manifests = tmp_path / "docs" / "agents"
    manifests.mkdir(parents=True)
    (tmp_path / "brain").mkdir()
    (manifests / "root-agent.yaml").write_text(yaml.safe_dump(ROOT_AGENT))
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("ROBOTHOR_MANIFEST_DIR", raising=False)
    snapshot = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    yield tmp_path
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == snapshot, "a child-tenant request touched the root workspace"


@pytest.fixture
def engine():
    fake = ChildEngine()
    # Both modules that dial the engine: the router (tools, reconcile, trigger)
    # and the manifest backend (the store calls).
    with (
        patch("routers.agent_manifests.engine_request", new=fake),
        patch("routers._manifest_backend.engine_request", new=fake),
        patch("routers._audit.log_event"),
    ):
        yield fake


def _client(role: str = "admin", *, home: str | None = None):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    client = TestClient(app, raise_server_exceptions=False)
    token = tokens.issue_access_token("alice", CHILD, role, home_tenant_id=home)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def test_the_listing_is_the_childs_fleet_not_the_roots(root_workspace, engine):
    body = _client().get("/api/agent-manifests").json()
    assert [a["id"] for a in body["agents"]] == ["child-agent"]
    assert body["default_agent"] == "child-agent"


def test_a_root_admin_acting_in_the_child_sees_the_childs_fleet(root_workspace, engine):
    body = _client(home=ROOT).get("/api/agent-manifests").json()
    assert [a["id"] for a in body["agents"]] == ["child-agent"]


def test_one_manifest_comes_from_the_childs_engine(root_workspace, engine):
    response = _client().get("/api/agent-manifests/child-agent")
    assert response.status_code == 200, response.text
    assert response.json()["manifest"]["id"] == "child-agent"
    assert response.json()["instructions"] == "# Child Agent\n"
    assert _client().get("/api/agent-manifests/root-agent").status_code == 404


def test_a_create_is_written_by_the_childs_engine(root_workspace, engine):
    response = _client().post(
        "/api/agent-manifests",
        json={"name": "New Helper", "description": "Helps", "tools_allowed": ["read_file"]},
    )
    assert response.status_code == 201, response.text
    assert "new-helper" in engine.files
    puts = [call for call in engine.calls if call[0] == "PUT"]
    assert puts and puts[0][1] == "/api/admin/manifests/new-helper"


def test_an_edit_round_trips_through_the_childs_engine(root_workspace, engine):
    response = _client().patch("/api/agent-manifests/child-agent", json={"name": "Renamed"})
    assert response.status_code == 200, response.text
    assert yaml.safe_load(engine.files["child-agent"])["name"] == "Renamed"


def test_retire_goes_to_the_childs_engine(root_workspace, engine):
    response = _client().request(
        "DELETE", "/api/agent-manifests/child-agent", json={"confirm": "child-agent"}
    )
    assert response.status_code == 200, response.text
    assert engine.retired == ["child-agent"]


def test_run_triggers_on_the_childs_engine(root_workspace, engine):
    response = _client().post("/api/agent-manifests/child-agent/run")
    assert response.status_code == 200, response.text
    assert ("POST", "/api/agents/child-agent/trigger", CHILD) in engine.calls


def test_the_fleet_view_is_the_childs(root_workspace, engine, monkeypatch):
    monkeypatch.setattr("routers.fleet.engine_request", engine, raising=False)
    with (
        patch("routers.fleet._schedule_rows", return_value={}),
        patch("routers.fleet._run_stats", return_value={}),
    ):
        body = _client().get("/api/fleet").json()
    assert [row["agent_id"] for row in body] == ["child-agent"]


def test_automations_are_the_childs(root_workspace, engine):
    with (
        patch("routers.automations._schedule_rows", return_value=[]),
        patch("routers.automations._latest_runs", return_value={}),
    ):
        body = _client().get("/api/automations").json()
    assert {row["agent_id"] for row in body["automations"]} == {"child-agent"}


# ─── An engine that predates the manifest routes ──────────────────────


class OldEngine(ChildEngine):
    """A child engine without ``/api/admin/manifests*``: FastAPI's own 404."""

    async def __call__(self, method, path, *, json=None, timeout=30, tenant=None):
        if path.startswith("/api/admin/manifests"):
            self.calls.append((method, path, tenant))
            return 404, {"detail": "Not Found"}
        return await super().__call__(method, path, json=json, timeout=timeout, tenant=tenant)


@pytest.fixture
def old_engine():
    fake = OldEngine()
    with (
        patch("routers.agent_manifests.engine_request", new=fake),
        patch("routers._manifest_backend.engine_request", new=fake),
        patch("routers._audit.log_event"),
    ):
        yield fake


def test_an_engine_without_the_manifest_routes_is_a_502_naming_the_upgrade(
    root_workspace, old_engine, caplog
):
    import logging

    with caplog.at_level(logging.WARNING, logger="routers._manifest_backend"):
        response = _client().get("/api/agent-manifests")
    assert response.status_code == 502
    assert "upgrade the engine" in response.json()["detail"]
    assert any("manifest administration" in r.getMessage() for r in caplog.records)


def test_a_single_agent_read_on_an_old_engine_is_not_reported_as_no_such_agent(
    root_workspace, old_engine
):
    response = _client().get("/api/agent-manifests/child-agent")
    assert response.status_code == 502
    assert "upgrade the engine" in response.json()["detail"]


def test_a_missing_agent_on_a_current_engine_is_still_no_such_agent(root_workspace, engine):
    response = _client().get("/api/agent-manifests/nobody")
    assert response.status_code == 404
    assert response.json()["detail"] == "no such agent"
