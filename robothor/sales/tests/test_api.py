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

        def settings_snapshot(self):
            return {"config": {"daily_limit_units": 0}, "revision": 3}

        def configure(self, changes, actor, **review):
            calls.append((self.tenant, changes, actor, review))

        def retry_provider_read(self, job_id, actor, reason):
            calls.append((self.tenant, job_id, actor, reason))

        def provider_reads(self, **filters):
            calls.append((self.tenant, filters))
            return {"items": [], "next_cursor": None}

        def reassign_business_customer(self, observation_id, **review):
            calls.append((self.tenant, observation_id, review))

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


def test_reviewed_pilot_limits_require_human_revision_and_cannot_enable_integrations(client):
    c, calls = client
    path = "/api/sales/settings/review"
    body = {
        "changes": {"monthly_limit_units": 500_000_000},
        "expected_revision": 3,
        "reason": "Reviewed monthly pilot budget",
    }
    for headers in (
        {},
        {"x-test-role": "member"},
        {"x-test-role": "admin", "x-test-service": "yes"},
    ):
        assert c.post(path, headers=headers, json=body).status_code == 403
        assert c.get("/api/sales/settings", headers=headers).status_code == 403
    headers = {"x-test-role": "admin"}
    assert c.get("/api/sales/settings", headers=headers).json()["revision"] == 3
    for extra in (
        {"actor": "operator:forged"},
        {"expected_revision": True},
        {"changes": {}},
        {"changes": {"sending_enabled": True}},
        {"changes": {"agents": {"scout": "other"}}},
        {"changes": {"active_knowledge_version": "unreviewed"}},
    ):
        assert c.post(path, headers=headers, json={**body, **extra}).status_code == 422
    assert c.post(path, headers=headers, json=body).status_code == 200
    assert calls == [
        (
            "tenant-a",
            body["changes"],
            "operator:user-1",
            {"expected_revision": 3, "reason": body["reason"]},
        )
    ]


def test_provider_read_inventory_is_human_only_and_filters_cannot_select_writes(client):
    c, calls = client
    path = "/api/sales/provider-reads"
    assert c.get(path).status_code == 403
    assert c.get(path, headers={"x-test-role": "member"}).status_code == 403
    assert c.get(path, headers={"x-test-role": "admin", "x-test-service": "yes"}).status_code == 403
    assert c.get(path + "?kind=sales.email", headers={"x-test-role": "admin"}).status_code == 422
    assert c.get(
        path + "?state=all&kind=sales.business", headers={"x-test-role": "admin"}
    ).json() == {"items": [], "next_cursor": None}
    assert calls == [("tenant-a", {"state": "all", "kind": "sales.business", "after": None})]


def test_reassignment_requires_human_identity_and_exact_review_contract(client):
    c, calls = client
    identity = "00000000-0000-4000-8000-000000000001"
    body = {
        "expected_revision": "v1",
        "expected_binding_version": 1,
        "expected_prospect_id": "00000000-0000-4000-8000-000000000002",
        "target_prospect_id": "00000000-0000-4000-8000-000000000003",
        "reason": "Reviewed corrected practice ownership",
    }
    path = f"/api/sales/business-observations/{identity}/reassign"
    for headers in (
        {},
        {"x-test-role": "member"},
        {"x-test-role": "admin", "x-test-service": "yes"},
    ):
        assert c.post(path, json=body, headers=headers).status_code == 403
    headers = {"x-test-role": "admin"}
    assert (
        c.post(path, json={**body, "actor": "operator:forged"}, headers=headers).status_code == 422
    )
    assert (
        c.post(path, json={**body, "expected_binding_version": True}, headers=headers).status_code
        == 422
    )
    assert c.post(path, json=body, headers=headers).status_code == 200
    assert calls == [("tenant-a", identity, {**body, "actor": "operator:user-1"})]


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


def test_read_retry_uses_verified_human_tenant_and_requires_repair_reason(client):
    c, calls = client
    body = {"reason": "Workspace configuration repaired"}
    path = "/api/sales/jobs/00000000-0000-4000-8000-000000000001/retry"
    assert c.post(path, json=body).status_code == 403
    assert (
        c.post(
            path, json=body, headers={"x-test-role": "admin", "x-test-service": "yes"}
        ).status_code
        == 403
    )
    assert (
        c.post(
            path, json={**body, "tenant_id": "other"}, headers={"x-test-role": "admin"}
        ).status_code
        == 422
    )
    assert not calls
    assert c.post(path, json=body, headers={"x-test-role": "admin"}).status_code == 200
    assert calls == [
        ("tenant-a", "00000000-0000-4000-8000-000000000001", "operator:user-1", body["reason"])
    ]


def test_business_binding_requires_human_and_exact_reviewed_revision(client, monkeypatch):
    import robothor.sales.api as api

    c, calls = client
    monkeypatch.setattr(
        api.Sales,
        "bind_business_customer",
        lambda self, prospect, observation, revision, actor, reason: calls.append(
            (self.tenant, prospect, observation, revision, actor, reason)
        ),
        raising=False,
    )
    prospect = "00000000-0000-4000-8000-000000000001"
    observation = "00000000-0000-4000-8000-000000000002"
    path = f"/api/sales/prospects/{prospect}/business-customer"
    payload = {
        "observation_id": observation,
        "expected_revision": "version-1",
        "reason": "Reviewed public business identity",
    }
    assert (
        c.post(
            path, json=payload, headers={"x-test-role": "admin", "x-test-service": "yes"}
        ).status_code
        == 403
    )
    assert (
        c.post(
            path, json={**payload, "tenant_id": "other"}, headers={"x-test-role": "admin"}
        ).status_code
        == 422
    )
    assert c.post(path, json=payload, headers={"x-test-role": "admin"}).status_code == 200
    assert calls == [
        (
            "tenant-a",
            prospect,
            observation,
            "version-1",
            "operator:user-1",
            "Reviewed public business identity",
        )
    ]


def test_business_observation_list_is_bounded_and_operator_only(client, monkeypatch):
    import robothor.sales.api as api

    c, calls = client
    monkeypatch.setattr(
        api.Sales,
        "business_records",
        lambda self, **query: {"tenant": self.tenant, **query},
        raising=False,
    )
    path = "/api/sales/business-observations"
    assert c.get(path).status_code == 403
    assert c.get(path + "?kind=patient", headers={"x-test-role": "admin"}).status_code == 422
    response = c.get(
        path + "?kind=practice&source=orders_app&account_id=one", headers={"x-test-role": "admin"}
    )
    assert response.status_code == 200
    assert response.json()["tenant"] == "tenant-a"
