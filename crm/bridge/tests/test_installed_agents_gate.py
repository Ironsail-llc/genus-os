"""Installing, updating and removing a marketplace agent is an operator act.

These three routes write to the appliance's agent manifest directory and run
the template installer.  They shipped with no ``require_operator`` gate and no
audit row, so any verified member session in the primary tenant could add or
delete an agent and leave nothing behind to attribute it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from robothor.auth import tokens


@pytest.fixture(autouse=True)
def auth_key(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


def _client(role: str, *, user_id: str = "human-1") -> TestClient:
    """A bare ``TestClient`` carrying one verified human session.

    Not context-managed, for the same reason as ``controls_client_as_operator``
    in conftest: entering ``TestClient`` runs the real ASGI lifespan.
    """
    from bridge_service import app
    from routers._operator import PLATFORM_TENANT

    token = tokens.issue_access_token(user_id, PLATFORM_TENANT, role)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture
def installer():
    """Stub out the hub + installer so no test ever writes a manifest."""
    metadata = {"sha256": "a" * 64}
    hub = MagicMock()
    hub.get_bundle.return_value = metadata
    hub.download_bundle.return_value = "/tmp/bundle"
    hub_client = MagicMock()
    hub_client.return_value.__enter__.return_value = hub

    with (
        patch("robothor.templates.hub_client.HubClient", hub_client),
        patch("robothor.templates.hub_client.trusted_bundle_sha256", return_value="a" * 64),
        patch(
            "robothor.templates.installer.install",
            return_value={"agent_id": "note-taker", "files_created": []},
        ) as install,
        patch(
            "robothor.templates.installer.update",
            return_value={"version": "2.0.0"},
        ) as update,
        patch("robothor.templates.installer.remove") as remove,
        patch("routers._audit.log_event") as log_event,
    ):
        yield MagicMock(install=install, update=update, remove=remove, log_event=log_event)


@pytest.mark.parametrize("role", ["member", "user", "viewer", "auditor"])
def test_non_operator_human_cannot_install(installer, role):
    response = _client(role).post(
        "/api/installed-agents/install", json={"slug": "note-taker", "variables": {}}
    )
    assert response.status_code == 403
    installer.install.assert_not_called()


@pytest.mark.parametrize("role", ["member", "viewer"])
def test_non_operator_human_cannot_update_or_remove(installer, role):
    client = _client(role)
    assert client.post("/api/installed-agents/note-taker/update").status_code == 403
    assert client.delete("/api/installed-agents/note-taker").status_code == 403
    installer.update.assert_not_called()
    installer.remove.assert_not_called()


def test_operator_installs_and_the_act_is_audited(installer):
    response = _client("owner").post(
        "/api/installed-agents/install", json={"slug": "note-taker", "variables": {}}
    )

    assert response.status_code == 200
    installer.install.assert_called_once()
    assert installer.log_event.call_count == 1
    assert installer.log_event.call_args.args[0] == "helm.agent.install"
    assert installer.log_event.call_args.kwargs["action"] == "note-taker"
    assert installer.log_event.call_args.kwargs["actor"] == "human-1"


def test_operator_updates_and_the_act_is_audited(installer):
    response = _client("admin").post("/api/installed-agents/note-taker/update")

    assert response.status_code == 200
    installer.update.assert_called_once()
    assert installer.log_event.call_args.args[0] == "helm.agent.update"
    assert installer.log_event.call_args.kwargs["action"] == "note-taker"


def test_operator_removes_and_the_act_is_audited(installer):
    response = _client("owner").delete("/api/installed-agents/note-taker")

    assert response.status_code == 200
    installer.remove.assert_called_once_with("note-taker")
    assert installer.log_event.call_args.args[0] == "helm.agent.remove"
    assert installer.log_event.call_args.kwargs["action"] == "note-taker"


def test_a_failed_install_is_audited_as_an_error(installer):
    installer.install.side_effect = RuntimeError("boom")

    response = _client("owner").post(
        "/api/installed-agents/install", json={"slug": "note-taker", "variables": {}}
    )

    assert response.status_code == 500
    assert installer.log_event.call_args.kwargs["status"] == "error"


def test_the_audit_row_never_carries_install_variables(installer):
    """Marketplace ``variables`` routinely carry API keys — they are the reason
    ``audited`` takes ids and names only."""
    _client("owner").post(
        "/api/installed-agents/install",
        json={"slug": "note-taker", "variables": {"API_KEY": "sk-live-do-not-disclose"}},
    )

    assert "sk-live-do-not-disclose" not in repr(installer.log_event.call_args)


def test_a_service_token_cannot_install(installer, monkeypatch):
    """Agents administering their own fleet is exactly what the gate forbids."""
    from bridge_service import app
    from routers._operator import PLATFORM_TENANT

    monkeypatch.setattr("middleware.check_endpoint_access", lambda *a, **k: True)
    token = tokens.issue_service_token(
        "email-classifier",
        PLATFORM_TENANT,
        agent_id="email-classifier",
        scopes=("bridge:read", "bridge:write", "agent:admin"),
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})

    response = client.post(
        "/api/installed-agents/install", json={"slug": "note-taker", "variables": {}}
    )

    assert response.status_code == 403
    installer.install.assert_not_called()
