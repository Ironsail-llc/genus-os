"""Only the authenticated linked owner can retrieve private audit text."""

from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from crm.bridge.routers import autonomy
from robothor.autonomy.models import Scope
from robothor.autonomy.terms_audit import TermsAudit
from robothor.autonomy.tests.test_terms_audit import operation, snapshot


def test_owner_audit_routes_are_private_and_generic_on_failure(store, identity, monkeypatch):
    op = operation(store, identity)
    row = TermsAudit(store).record(identity, op["id"], "main", snapshot())
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
    path = f"/api/autonomy/operations/{op['id']}/terms"
    with TestClient(app) as client:
        listing = client.get(path)
        assert listing.status_code == 200
        assert "Private applicant" not in listing.text
        detail = client.get(path + "/" + row["id"])
        assert detail.status_code == 200 and "Private applicant" in detail.text
        assert detail.headers["cache-control"] == "no-store"
        auth.actor_id = "other-owner"
        assert client.get(path).status_code == 404
        assert client.get(path + "/" + row["id"]).status_code == 404
        auth.actor_id = identity.owner_id
        auth.is_service = True
        assert client.get(path + "/" + row["id"]).status_code == 403


def test_the_owner_can_erase_the_record_and_nobody_else_can(store, identity, monkeypatch):
    """`grep -rn "DELETE FROM autonomy"` returned nothing and the routes
    offered `DELETE /resources/{id}` and `DELETE /grants/{id}` and no way at
    all to remove the rendered review page — the owner's name, date of birth,
    address and the answers they gave a website."""
    op = operation(store, identity)
    row = TermsAudit(store).record(identity, op["id"], "main", snapshot())
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
    path = f"/api/autonomy/operations/{op['id']}/terms"
    with TestClient(app) as client:
        auth.actor_id = "other-owner"
        assert client.delete(path).json()["erased"] == 0
        auth.actor_id = identity.owner_id
        assert client.get(path + "/" + row["id"]).status_code == 200

        auth.is_service = True
        assert client.delete(path).status_code == 403
        auth.is_service = False

        assert client.delete(path).json()["erased"] == 1
        # Erasing twice is a success, and the audit fact outlives the content.
        assert client.delete(path).json()["erased"] == 0
        after = client.get(path + "/" + row["id"])
        assert after.status_code == 200
        assert "Private applicant" not in after.text
        assert after.json()["snapshot"] is None
        assert after.json()["redacted_at"]
        assert client.get(path).json()["snapshots"][0]["redacted_at"]
