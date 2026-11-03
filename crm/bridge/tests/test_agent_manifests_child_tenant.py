"""The agent builder, fleet and automations for a CHILD tenant.

A child tenant's manifests live on its own engine cell's volume, which the
bridge cannot reach. So for any acting tenant other than the platform tenant,
every builder call goes to THAT tenant's engine (``/api/admin/agent-manifests*``,
with a control token minted for it), the read-only fleet and automations views
ask that engine for its scan, and the bridge's own workspace — the ROOT fleet —
is never read or written. Showing a child admin the root fleet would be a leak;
writing a child's agent into it would hand the root engine an agent nobody in
the root tenant made.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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


class _ChildScheduler:
    def __init__(self, engine: ChildEngine) -> None:
        self._engine = engine

    async def reconcile(self):
        from robothor.engine.schedule_reconcile import ReconcileResult

        self._engine.reconciles += 1
        return ReconcileResult(added=[], replaced=[], pruned=[], blocked={}, clean=True)


class ChildEngine:
    """The child tenant's engine cell: the engine's REAL routes, on its own volume.

    Served in-process (ASGI) against ``workspace``, a directory the bridge's
    environment never names, so the root workspace is provably not where a
    child's manifest is read or written. Every call asserts it was addressed to
    the CHILD's engine; a call for any other tenant fails the test.
    """

    def __init__(self, workspace: Path, monkeypatch) -> None:
        from fastapi import FastAPI

        from robothor.auth.deps import AuthContext
        from robothor.engine import admin_manifests

        self.workspace = workspace
        self.calls: list[tuple[str, str, str | None]] = []
        self.triggered: list[str] = []
        self.reconciles = 0
        monkeypatch.setattr(
            "robothor.engine.admin_scheduler.registered_tool_names", lambda: ["read_file"]
        )
        monkeypatch.setattr("robothor.engine.manual_trigger.trigger_agent", self._trigger)

        app = FastAPI()

        @app.middleware("http")
        async def _as_the_bridge(request, call_next):
            request.state.auth = AuthContext(
                user_id="genus-bridge",
                tenant_id=CHILD,
                role="service",
                typ="service",
                scopes=frozenset({"engine:control"}),
            )
            return await call_next(request)

        admin_manifests.register(app, scheduler=_ChildScheduler(self), runner=MagicMock())
        self.app = app

    async def _trigger(self, runner, manifest_dir, agent_id, auth):
        self.triggered.append(agent_id)
        return {"status": "triggered", "agent_id": agent_id}

    @property
    def files(self) -> dict[str, str]:
        directory = self.workspace / "docs" / "agents"
        return {path.stem: path.read_text() for path in directory.glob("*.yaml")}

    async def __call__(self, method, path, *, json=None, timeout=30, tenant=None):
        import httpx

        self.calls.append((method, path, tenant))
        assert tenant == CHILD, f"{method} {path} reached the engine of {tenant!r}"
        # This engine's own environment, for the length of the call: the
        # cell's workspace and its own main agent.
        saved = {k: os.environ.get(k) for k in ("ROBOTHOR_WORKSPACE", "ROBOTHOR_MAIN_SESSION_KEY")}
        os.environ["ROBOTHOR_WORKSPACE"] = str(self.workspace)
        os.environ["ROBOTHOR_MAIN_SESSION_KEY"] = "agent:child-agent:primary"
        try:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://cell.test") as c:
                response = await c.request(method, path, json=json)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return response.status_code, response.json()


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    # Acting tokens are honoured only while switching is enforced.
    monkeypatch.setenv("GENUS_TENANT_SWITCH_MODE", "enforce")
    import routers._operator as operator

    from robothor.auth import tokens
    from robothor.settings import reset_settings

    monkeypatch.setattr(operator, "PLATFORM_TENANT", ROOT)
    tokens.reset_signing_key_cache()
    reset_settings()
    yield
    tokens.reset_signing_key_cache()
    reset_settings()


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
def child_workspace(tmp_path_factory):
    """The child engine cell's own volume."""
    workspace = tmp_path_factory.mktemp("child-cell")
    manifests = workspace / "docs" / "agents"
    manifests.mkdir(parents=True)
    (workspace / "brain").mkdir()
    (manifests / "child-agent.yaml").write_text(yaml.safe_dump(CHILD_AGENT, sort_keys=False))
    (manifests / "_defaults.yaml").write_text(f"model:\n  primary: {CHILD_MODEL}\n")
    (workspace / "brain" / "CHILD_AGENT.md").write_text("# Child Agent\n")
    return workspace


@pytest.fixture
def engine(child_workspace, monkeypatch):
    fake = ChildEngine(child_workspace, monkeypatch)
    # Both modules that dial the engine: the builder's router, and the scan the
    # fleet and automations views read.
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
    assert ("POST", "/api/admin/agent-manifests", CHILD) in engine.calls
    # Scaffolded from the CHILD's fleet default, not the root's.
    assert yaml.safe_load(engine.files["new-helper"])["model"]["primary"] == CHILD_MODEL
    assert (engine.workspace / "brain" / "NEW_HELPER.md").is_file()


def test_an_edit_round_trips_through_the_childs_engine(root_workspace, engine):
    response = _client().patch("/api/agent-manifests/child-agent", json={"name": "Renamed"})
    assert response.status_code == 200, response.text
    assert yaml.safe_load(engine.files["child-agent"])["name"] == "Renamed"


def test_retire_goes_to_the_childs_engine(root_workspace, engine):
    response = _client().request(
        "DELETE", "/api/agent-manifests/child-agent", json={"confirm": "child-agent"}
    )
    assert response.status_code == 200, response.text
    assert "child-agent" not in engine.files
    assert (engine.workspace / "docs" / "agents" / "retired" / "child-agent.yaml").is_file()


def test_run_triggers_on_the_childs_engine(root_workspace, engine):
    response = _client().post("/api/agent-manifests/child-agent/run")
    assert response.status_code == 200, response.text
    assert ("POST", "/api/admin/agent-manifests/child-agent/run", CHILD) in engine.calls
    assert engine.triggered == ["child-agent"]


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
    """A child engine without the manifest routes: FastAPI's own 404."""

    async def __call__(self, method, path, *, json=None, timeout=30, tenant=None):
        if path.startswith(("/api/admin/agent-manifests", "/api/admin/manifests")):
            self.calls.append((method, path, tenant))
            return 404, {"detail": "Not Found"}
        return await super().__call__(method, path, json=json, timeout=timeout, tenant=tenant)


@pytest.fixture
def old_engine(child_workspace, monkeypatch):
    fake = OldEngine(child_workspace, monkeypatch)
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

    with caplog.at_level(logging.WARNING, logger="routers.agent_manifests"):
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
