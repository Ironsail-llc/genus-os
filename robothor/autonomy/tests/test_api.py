"""Real HTTP parsing: neither validation nor authorization may echo secrets."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from crm.bridge.routers import autonomy
from robothor.autonomy.models import Scope


@pytest.fixture
def api(monkeypatch):
    app = FastAPI()
    identity = SimpleNamespace(tenant_id="test", actor_id="alice", role="owner", is_service=False)
    store = MagicMock()
    store.put_resource.return_value = {"id": "reference", "kind": "credential"}
    monkeypatch.setattr(autonomy, "AutonomyStore", lambda: store)
    monkeypatch.setattr(
        autonomy, "scope_for_actor", lambda tenant, actor: Scope(tenant_id=tenant, owner_id=actor)
    )

    @app.middleware("http")
    async def auth(request: Request, call_next):
        request.state.auth = identity
        return await call_next(request)

    app.include_router(autonomy.router)
    with TestClient(app) as client:
        yield client, identity, store


def test_resource_enrollment_is_write_only_and_owner_is_server_bound(api):
    client, _, store = api
    response = client.post(
        "/api/autonomy/resources",
        json={
            "kind": "credential",
            "label": "Login",
            "origin": "https://shop.example",
            "payload": '{"password":"private-password","username":"alice"}',
        },
    )
    assert response.status_code == 200
    assert "private-password" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert store.put_resource.call_args.args[0] == Scope(tenant_id="test", owner_id="alice")


@pytest.mark.parametrize("service,role", [(True, "owner"), (False, "viewer"), (False, "auditor")])
def test_service_and_read_only_accounts_cannot_enroll_or_grant(api, service, role):
    client, identity, store = api
    identity.is_service = service
    identity.role = role
    for path in ("resources", "grants", "settings"):
        response = client.request(
            "PUT" if path == "settings" else "POST",
            f"/api/autonomy/{path}",
            json={"payload": "private-password"},
        )
        assert response.status_code == 403
        assert "private-password" not in response.text
    store.put_resource.assert_not_called()
    store.create_grant.assert_not_called()


def test_invalid_payload_does_not_echo_input_or_accept_owner_override(api):
    client, _, store = api
    response = client.post(
        "/api/autonomy/resources",
        json={
            "kind": "credential",
            "label": "Login",
            "owner_id": "bob",
            "payload": "4242424242424242",
        },
    )
    assert response.status_code == 422
    assert "4242424242424242" not in response.text
    store.put_resource.assert_not_called()
