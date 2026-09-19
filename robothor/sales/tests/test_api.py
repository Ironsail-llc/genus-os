"""Human decisions derive their tenant and actor from verified authentication."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from robothor.sales.api import router


@pytest.fixture
def client(monkeypatch):
    import robothor.sales.api as api

    calls = []

    class Service:
        def __init__(self, tenant):
            self.tenant = tenant
            self.ops = self

        def overview(self):
            return {"tenant": self.tenant}

        def decide(self, action, approved, actor):
            calls.append((self.tenant, action, approved, actor))

    monkeypatch.setattr(api, "Sales", Service)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        if "x-test-role" in request.headers:
            request.state.auth = SimpleNamespace(
                role=request.headers["x-test-role"],
                is_service=request.headers.get("x-test-service") == "yes",
                tenant_id="tenant-a",
                actor_id="user-1",
            )
        return await call_next(request)

    app.include_router(router)
    return TestClient(app), calls


@pytest.mark.parametrize(
    "headers", [{}, {"x-test-role": "member"}, {"x-test-role": "admin", "x-test-service": "yes"}]
)
def test_agents_and_unverified_callers_cannot_approve(client, headers):
    c, calls = client
    assert (
        c.post(
            "/api/sales/actions/action-1/decision", json={"approved": True}, headers=headers
        ).status_code
        == 403
    )
    assert not calls


def test_tenant_and_actor_are_not_caller_supplied(client):
    c, calls = client
    h = {"x-test-role": "admin"}
    assert c.get("/api/sales", headers=h).json() == {"tenant": "tenant-a"}
    assert (
        c.post(
            "/api/sales/actions/action-1/decision",
            headers=h,
            json={"approved": True, "actor": "operator:forged", "tenant_id": "tenant-b"},
        ).status_code
        == 422
    )
    assert (
        c.post(
            "/api/sales/actions/action-1/decision", headers=h, json={"approved": True}
        ).status_code
        == 200
    )
    assert calls == [("tenant-a", "action-1", True, "operator:user-1")]
