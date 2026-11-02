"""The engine's agent-manifest routes under ``/api/admin/agent-manifests``.

The bridge's agent builder proxies here. What these tests hold down is the part
that only the ENGINE can do, because only the engine has the workspace, the
tool registry and the job set:

* the routes are mounted on the engine's real app, behind ``engine:control``
  like every other ``/api/admin`` route, and a bridge control token passes;
* a write is reconciled IN-PROCESS, and a reconcile that cannot happen is
  reported as ``applied: false`` rather than swallowed or raised;
* validation uses the engine's own registry, and a registry that will not
  answer downgrades to a warning instead of blocking the save;
* ``run`` fires the in-process trigger, and refuses an id that is not an agent.

The full behavioural matrix (gates, repairs, wrong-typed manifests, hostile
ids) runs through the bridge in ``crm/bridge/tests/test_agent_manifests_endpoint.py``,
which serves these routes in-process against a tmp workspace.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi import APIRouter, FastAPI
from starlette.testclient import TestClient

from robothor.engine.schedule_reconcile import ReconcileResult

EXISTING = {
    "id": "demo-agent",
    "name": "Demo Agent",
    "description": "Does the demo work",
    "version": "2026-09-01",
    "department": "operations",
    "model": {"primary": "openrouter/example/demo-model"},
    "schedule": {"cron": "0 9 * * *", "timezone": "UTC"},
    "delivery": {"mode": "none"},
    "tools_allowed": ["exec", "read_file", "write_file"],
    "instruction_file": "brain/DEMO_AGENT.md",
}

TOOLS = ["exec", "read_file", "write_file", "search_memory", "list_my_tasks"]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from pathlib import Path

    manifests = tmp_path / "docs" / "agents"
    manifests.mkdir(parents=True)
    (tmp_path / "brain").mkdir()
    schema = Path(__file__).resolve().parents[1] / "schema" / "agent_manifest.yaml"
    (manifests / "schema.yaml").write_bytes(schema.read_bytes())
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("ROBOTHOR_MANIFEST_DIR", raising=False)
    return tmp_path


@pytest.fixture
def seeded(workspace):
    path = workspace / "docs" / "agents" / "demo-agent.yaml"
    path.write_text(yaml.safe_dump(EXISTING, sort_keys=False))
    (workspace / "brain" / "DEMO_AGENT.md").write_text(
        "# Demo Agent\n\n## Your Role\n\nDemo.\n\n## Tasks\n\n1. Work.\n\n## Output\n\nA line.\n"
    )
    return path


@pytest.fixture
def fake_scheduler():
    scheduler = MagicMock()
    scheduler.reconcile = AsyncMock(
        return_value=ReconcileResult(
            added=["demo-agent"], replaced=[], pruned=[], blocked={}, clean=True
        )
    )
    return scheduler


@pytest.fixture
def tools():
    with patch(
        "robothor.engine.admin_scheduler.registered_tool_names", return_value=list(TOOLS)
    ) as names:
        yield names


def _bare_app(scheduler=None, runner=None) -> FastAPI:
    """Only the manifest routes, with the identity the auth middleware would set."""
    from robothor.auth.deps import AuthContext
    from robothor.engine import admin_manifests

    app = FastAPI()

    @app.middleware("http")
    async def _as_bridge(request, call_next):
        request.state.auth = AuthContext(
            user_id="genus-bridge",
            tenant_id="test-tenant",
            role="service",
            typ="service",
            scopes=frozenset({"engine:control"}),
        )
        return await call_next(request)

    admin_manifests.register(app, scheduler=scheduler, runner=runner)
    return app


@pytest.fixture
def client(workspace, fake_scheduler, tools):
    return TestClient(_bare_app(fake_scheduler, runner=MagicMock()))


def _create_body(**overrides):
    body = {
        "name": "Invoice Watcher",
        "description": "Watches invoices",
        "department": "operations",
        "model": "openrouter/example/demo-model",
        "cron": "0 8 * * *",
        "timezone": "UTC",
        "tools_allowed": ["exec", "read_file", "write_file"],
    }
    body.update(overrides)
    return body


# ─── Mounted, and behind engine:control ──────────────────────────────


def _health_app():
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
        return create_health_app(mock_config, runner=None, scheduler=None)


class TestAuthentication:
    @pytest.fixture
    def _signed_tokens(self, monkeypatch):
        from robothor.auth import tokens

        monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
        monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long")
        tokens.reset_signing_key_cache()
        yield tokens
        tokens.reset_signing_key_cache()

    def test_the_routes_are_mounted_on_the_engine_app(self):
        # FastAPI keeps an included router as a lazy route group, so flatten
        # it the way ``crm/bridge/tests/test_route_concurrency.py`` does.
        paths = {
            getattr(candidate, "path", "")
            for route in _health_app().routes
            for candidate in getattr(getattr(route, "original_router", None), "routes", (route,))
        }

        assert "/api/admin/agent-manifests" in paths
        assert "/api/admin/agent-manifests/{agent_id}/run" in paths

    def test_an_unauthenticated_call_is_refused(self, _signed_tokens, seeded):
        client = TestClient(_health_app(), raise_server_exceptions=False)

        assert client.get("/api/admin/agent-manifests").status_code == 401
        response = client.post("/api/admin/agent-manifests", json=_create_body())
        assert response.status_code == 401
        assert not (seeded.parent / "invoice-watcher.yaml").exists()

    def test_a_read_scoped_token_is_refused(self, _signed_tokens, workspace):
        from robothor.engine.auth import ENGINE_AUDIENCE

        token = _signed_tokens.issue_service_token(
            "genus-bridge",
            "test-tenant",
            audience=ENGINE_AUDIENCE,
            scopes=("engine:read",),
            ttl_seconds=60,
        )
        client = TestClient(_health_app(), raise_server_exceptions=False)

        response = client.get(
            "/api/admin/agent-manifests", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 403

    def test_the_bridges_own_control_token_is_admitted(self, _signed_tokens, seeded):
        """The token ``routers/_engine_client.py`` mints, minted the same way."""
        from robothor.engine_control import control_token

        token = control_token("genus-bridge", "test-tenant", require_existing_key=False)
        client = TestClient(_health_app(), raise_server_exceptions=False)

        response = client.get(
            "/api/admin/agent-manifests", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()["agents"]] == ["demo-agent"]


# ─── Reading from the engine's workspace ─────────────────────────────


class TestReading:
    def test_the_listing_is_the_engines_workspace(self, client, seeded):
        body = client.get("/api/admin/agent-manifests").json()

        assert [row["id"] for row in body["agents"]] == ["demo-agent"]
        assert body["count"] == 1
        assert body["broken"] == []
        assert "default_agent" in body

    def test_one_agent_comes_back_with_its_instructions_and_verdict(self, client, seeded):
        body = client.get("/api/admin/agent-manifests/demo-agent").json()

        assert body["manifest"]["id"] == "demo-agent"
        assert "## Your Role" in body["instructions"]
        assert body["validation"]["ok"] is True, body["validation"]

    def test_an_absent_agent_is_a_404(self, client, workspace):
        assert client.get("/api/admin/agent-manifests/nobody").status_code == 404

    def test_a_hostile_id_is_a_422(self, client, workspace):
        assert client.get("/api/admin/agent-manifests/A").status_code == 422


# ─── Validation in the engine, against its own registry ──────────────


class TestValidation:
    def test_it_checks_tool_names_against_the_engines_registry(self, client, workspace, tools):
        candidate = {**EXISTING, "tools_allowed": ["exec", "no_such_tool"]}

        body = client.post(
            "/api/admin/agent-manifests/validate", json={"manifest": candidate}
        ).json()

        assert body["ok"] is False
        tools.assert_called()
        assert any("no_such_tool" in issue["message"] for issue in body["errors"])

    def test_a_registry_that_will_not_answer_warns_instead_of_blocking(
        self, workspace, fake_scheduler
    ):
        """An operator must be able to edit an agent while the registry is
        unhappy — that is precisely when they want to."""
        with patch(
            "robothor.engine.admin_scheduler.registered_tool_names",
            side_effect=RuntimeError("a plugin exploded"),
        ):
            client = TestClient(_bare_app(fake_scheduler))
            body = client.post(
                "/api/admin/agent-manifests/validate", json={"manifest": EXISTING}
            ).json()

        assert body["ok"] is True, body
        assert any(issue["code"] == "tools_unverified" for issue in body["warnings"])
        assert "exploded" not in repr(body)


# ─── Writes reconcile in-process ─────────────────────────────────────


class TestReconcile:
    def test_a_create_writes_the_engines_workspace_and_reconciles(
        self, client, workspace, fake_scheduler
    ):
        response = client.post("/api/admin/agent-manifests", json=_create_body())

        assert response.status_code == 201, response.text
        assert (workspace / "docs" / "agents" / "invoice-watcher.yaml").is_file()
        assert (workspace / "brain" / "INVOICE_WATCHER.md").is_file()
        fake_scheduler.reconcile.assert_awaited_once()
        assert response.json()["reconcile"]["applied"] is True
        assert response.json()["reconcile"]["added"] == ["demo-agent"]

    def test_a_patch_reconciles(self, client, seeded, fake_scheduler):
        response = client.patch("/api/admin/agent-manifests/demo-agent", json={"name": "Renamed"})

        assert response.status_code == 200, response.text
        assert yaml.safe_load(seeded.read_text())["name"] == "Renamed"
        assert "pre_existing" in response.json()
        fake_scheduler.reconcile.assert_awaited_once()

    def test_disable_and_retire_reconcile(self, client, seeded, fake_scheduler):
        client.post("/api/admin/agent-manifests/demo-agent/disable")
        client.request(
            "DELETE", "/api/admin/agent-manifests/demo-agent", json={"confirm": "demo-agent"}
        )

        assert fake_scheduler.reconcile.await_count == 2
        assert (seeded.parent / "retired" / "demo-agent.yaml").is_file()

    def test_no_scheduler_is_reported_not_raised(self, workspace, tools):
        from robothor.engine import daemon

        with patch.object(daemon, "_ACTIVE_SCHEDULER", None):
            client = TestClient(_bare_app(scheduler=None))
            response = client.post("/api/admin/agent-manifests", json=_create_body())

        assert response.status_code == 201, response.text
        assert response.json()["reconcile"] == {
            "applied": False,
            "error": "scheduler not running",
        }

    def test_a_reconcile_that_raises_is_reported_without_its_text(
        self, workspace, tools, fake_scheduler
    ):
        fake_scheduler.reconcile = AsyncMock(side_effect=OSError(f"{workspace}/docs/agents"))
        client = TestClient(_bare_app(fake_scheduler))

        response = client.post("/api/admin/agent-manifests", json=_create_body())

        assert response.status_code == 201, response.text
        reconcile = response.json()["reconcile"]
        assert reconcile["applied"] is False
        assert reconcile["error"]
        assert str(workspace) not in response.text

    def test_a_refused_write_does_not_reconcile(self, client, workspace, fake_scheduler):
        response = client.post(
            "/api/admin/agent-manifests", json=_create_body(cron="every other tuesday")
        )

        assert response.status_code == 422
        fake_scheduler.reconcile.assert_not_awaited()


# ─── Run, in-process ─────────────────────────────────────────────────


class TestRun:
    def test_it_fires_the_in_process_trigger(self, workspace, seeded, fake_scheduler, tools):
        runner = MagicMock()
        trigger = AsyncMock(return_value={"status": "started", "agent_id": "demo-agent"})
        with patch("robothor.engine.manual_trigger.trigger_agent", new=trigger):
            client = TestClient(_bare_app(fake_scheduler, runner=runner))
            response = client.post("/api/admin/agent-manifests/demo-agent/run")

        assert response.status_code == 200, response.text
        assert response.json() == {
            "id": "demo-agent",
            "triggered": {"status": "started", "agent_id": "demo-agent"},
        }
        args = trigger.await_args.args
        assert args[0] is runner
        assert args[2] == "demo-agent"

    def test_an_unknown_agent_is_a_404_and_is_never_triggered(self, client, workspace):
        trigger = AsyncMock()
        with patch("robothor.engine.manual_trigger.trigger_agent", new=trigger):
            response = client.post("/api/admin/agent-manifests/nobody/run")

        assert response.status_code == 404
        trigger.assert_not_awaited()


class TestTheTriggerRouteStillWorks:
    """``POST /api/agents/{id}/trigger`` now delegates to the same function the
    manifest ``run`` route calls, so the two cannot drift."""

    def test_no_runner_is_still_reported(self):
        import asyncio

        from robothor.engine import manual_trigger

        result = asyncio.run(manual_trigger.trigger_agent(None, None, "demo-agent", None))

        assert result == {"error": "Runner not available"}


class TestAWorkspaceThatCannotBeWrittenIsARefusal:
    """A full or read-only workspace is the operator's to fix, and it should
    read as one: a 422 naming the rule, not a plain-text 500 the bridge can
    only report as "the engine did not answer"."""

    @pytest.fixture
    def unwritable(self, monkeypatch):
        import os

        def _full(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "fsync", _full)

    def test_a_create_on_a_full_workspace_is_a_422(self, client, workspace, unwritable):
        response = client.post("/api/admin/agent-manifests", json=_create_body())

        assert response.status_code == 422, response.text
        assert "not writable" in response.json()["detail"]
        assert str(workspace) not in response.text

    def test_a_patch_on_a_full_workspace_is_a_422(self, client, seeded, unwritable):
        before = seeded.read_text()

        response = client.patch("/api/admin/agent-manifests/demo-agent", json={"name": "X"})

        assert response.status_code == 422, response.text
        assert "not writable" in response.json()["detail"]
        assert seeded.read_text() == before

    def test_a_retire_on_an_unwritable_workspace_is_a_422(self, client, seeded, monkeypatch):
        from pathlib import Path

        def _refuse(*_args, **_kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "replace", _refuse)

        response = client.request(
            "DELETE", "/api/admin/agent-manifests/demo-agent", json={"confirm": "demo-agent"}
        )

        assert response.status_code == 422, response.text
        assert seeded.is_file()


class TestTheTriggerLogIsNotForgeable:
    def test_a_newline_in_the_id_cannot_split_the_log_record(self, caplog):
        import asyncio
        import logging

        from robothor.engine import manual_trigger
        from robothor.engine.manifest_schema import ManifestSchemaError

        with (
            patch(
                "robothor.engine.config.load_agent_config",
                side_effect=ManifestSchemaError("a\nforged", []),
            ),
            caplog.at_level(logging.ERROR, logger="robothor.engine.manual_trigger"),
        ):
            asyncio.run(manual_trigger.trigger_agent(MagicMock(), None, "a\nforged", None))

        assert caplog.records, "the schema refusal was not logged"
        assert all("\n" not in record.getMessage() for record in caplog.records)
