"""Payment status is private, read-only and excludes provider references."""

from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from crm.bridge.routers import autonomy
from robothor.autonomy.models import Scope
from robothor.autonomy.payment_journal import PaymentJournal
from robothor.autonomy.tests.test_payment_journal import charge, purchase


def test_payment_status_is_owner_only_and_read_only(store, identity, monkeypatch):
    op = purchase(store, identity)
    PaymentJournal(store).append(identity, op, charge())
    monkeypatch.setattr(autonomy, "AutonomyStore", lambda: store)
    monkeypatch.setattr(
        autonomy, "scope_for_actor", lambda tenant, actor: Scope(tenant_id=tenant, owner_id=actor)
    )
    auth = SimpleNamespace(
        tenant_id=identity.tenant_id, actor_id=identity.owner_id, role="owner", is_service=False
    )
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        request.state.auth = auth
        return await call_next(request)

    app.include_router(autonomy.router)
    path = f"/api/autonomy/operations/{op}/payment"
    with TestClient(app) as client:
        result = client.get(path)
        assert result.status_code == 200
        assert result.json()["position"]["charged_minor"] == 600
        assert result.headers["cache-control"] == "no-store"
        assert "issuer-private-reference" not in result.text
        assert client.post(path, json={"state": "refunded"}).status_code == 405
        auth.actor_id = "another-owner"
        assert client.get(path).status_code == 404
        auth.actor_id = identity.owner_id
        auth.is_service = True
        assert client.get(path).status_code == 403


async def test_agent_reads_only_its_own_payment_status(store, identity, monkeypatch):
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import autonomy as handler

    op = purchase(store, identity)
    PaymentJournal(store).append(identity, op, charge())
    monkeypatch.setattr(handler, "AutonomyStore", lambda: store)
    monkeypatch.setattr(handler, "scope_for_actor", lambda *args: identity)
    result = await handler.handle(
        {"kind": "payment_status", "operation_id": op},
        ToolContext(agent_id="main", tenant_id=identity.tenant_id, user_id="owner"),
    )
    assert result["position"]["state"] == "charged"
    denied = await handler.handle(
        {"kind": "payment_status", "operation_id": op},
        ToolContext(agent_id="other-agent", tenant_id=identity.tenant_id, user_id="owner"),
    )
    assert denied == {"error": "agent_not_allowed"}
