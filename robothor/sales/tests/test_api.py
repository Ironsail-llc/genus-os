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

        def library(self, **query):
            calls.append((self.tenant, query))
            return {"items": [], "next_cursor": None}

        def select_library(self, **selection):
            calls.append((self.tenant, selection))

        def publish_library(self, packet, **review):
            calls.append((self.tenant, packet, review))

        def library_record(self, **query):
            calls.append((self.tenant, query))
            return

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

    class CalibrationStub:
        def __init__(self, service):
            self.tenant = service.tenant

        def create(self, **body):
            calls.append((self.tenant, body))
            return {"id": "cohort-1"}

        def assess(self, item_id, **body):
            calls.append((self.tenant, item_id, body))
            return {"revision": 1}

        def list_cohorts(self, **query):
            calls.append((self.tenant, query))
            return {"items": [], "next_cursor": None}

    monkeypatch.setattr(api, "Calibration", CalibrationStub, raising=False)
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


def test_qualification_assessment_is_human_scoped_without_promotion_authority(client):
    c, calls = client
    body = {
        "reference_decision": "qualified",
        "expected_snapshot_hash": "a" * 64,
        "expected_assessment_id": None,
        "reason": "This practice meets our business criteria",
    }
    item = "00000000-0000-4000-8000-000000000001"
    path = f"/api/sales/calibration/items/{item}/assessment"
    for headers in (
        {},
        {"x-test-role": "member"},
        {"x-test-role": "admin", "x-test-service": "yes"},
    ):
        assert c.post(path, headers=headers, json=body).status_code == 403
        assert c.get("/api/sales/calibration", headers=headers).status_code == 403
    headers = {"x-test-role": "admin"}
    assert c.post(path, json={**body, "approved": True}, headers=headers).status_code == 422
    assert c.post(path, json={**body, "actor": "forged"}, headers=headers).status_code == 422
    assert c.post(path, json=body, headers=headers).status_code == 200
    assert calls.pop() == ("tenant-a", item, {**body, "actor": "operator:user-1"})
    create = {
        "name": "Pilot review",
        "target_size": 100,
        "agreement_target_percent": 85,
        "expected_settings_revision": 3,
        "reason": "Assess the first researched sample",
    }
    assert c.post("/api/sales/calibration", json=create, headers=headers).status_code == 200
    assert calls == [("tenant-a", {**create, "actor": "operator:user-1"})]


def test_library_preview_and_publication_require_a_human_and_exact_review_hash(client):
    c, calls = client
    record_path = "/api/sales/library/records/knowledge/claims-1"
    assert c.get(record_path).status_code == 403
    assert c.get(record_path, headers={"x-test-role": "admin"}).status_code == 404
    assert calls.pop() == ("tenant-a", {"kind": "knowledge", "version": "claims-1"})
    packet = {
        "kind": "knowledge",
        "version": "claims-1",
        "data": {"claims": {"access": "One pharmacy workflow."}},
    }
    for headers in (
        {},
        {"x-test-role": "member"},
        {"x-test-role": "admin", "x-test-service": "yes"},
    ):
        assert c.post("/api/sales/library/preview", json=packet, headers=headers).status_code == 403
    headers = {"x-test-role": "admin"}
    response = c.post("/api/sales/library/preview", json=packet, headers=headers)
    assert response.status_code == 200
    reviewed = response.json()
    assert reviewed["packet"] == packet and len(reviewed["content_hash"]) == 64
    body = {
        "packet": packet,
        "expected_hash": reviewed["content_hash"],
        "reason": "Reviewed source-backed claims",
    }
    path = "/api/sales/library/publication"
    assert c.post(path, json=body).status_code == 403
    assert c.post(path, json={**body, "actor": "forged"}, headers=headers).status_code == 422
    assert (
        c.post(path, json={**body, "expected_hash": "invalid"}, headers=headers).status_code == 422
    )
    assert c.post(path, json=body, headers=headers).status_code == 200
    assert calls == [
        (
            "tenant-a",
            packet,
            {
                "expected_hash": reviewed["content_hash"],
                "reason": body["reason"],
                "actor": "operator:user-1",
            },
        )
    ]


def test_library_catalog_and_selection_are_human_scoped(client):
    c, calls = client
    body = {
        "policy_versions": {"network_access": "v1"},
        "knowledge_version": "v1",
        "expected_revision": 3,
        "reason": "Reviewed library selection",
    }
    path = "/api/sales/library/selection"
    for headers in (
        {},
        {"x-test-role": "member"},
        {"x-test-role": "admin", "x-test-service": "yes"},
    ):
        assert c.get("/api/sales/library?kind=knowledge", headers=headers).status_code == 403
        assert c.post(path, json=body, headers=headers).status_code == 403
    headers = {"x-test-role": "admin"}
    assert (
        c.get(
            "/api/sales/library?kind=qualification&after=v1&limit=20", headers=headers
        ).status_code
        == 200
    )
    assert calls.pop() == ("tenant-a", {"kind": "qualification", "after": "v1", "limit": 20})
    for extra in (
        {"actor": "operator:forged"},
        {"sending_enabled": True},
        {"expected_revision": True},
    ):
        assert c.post(path, json={**body, **extra}, headers=headers).status_code == 422
    assert c.post(path, json=body, headers=headers).status_code == 200
    assert calls == [("tenant-a", {**body, "actor": "operator:user-1"})]


def test_reviewed_pilot_limits_require_human_revision_and_cannot_enable_integrations(client):
    c, calls = client
    path = "/api/sales/settings/review"
    body = {
        "changes": {"monthly_limit_units": 500_000_000, "followup_delays_business_days": [3, 4]},
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


def test_research_request_routes_are_human_scoped_and_do_not_accept_authority(client, monkeypatch):
    import robothor.sales.api as api

    c, calls = client

    class RequestsStub:
        def __init__(self, service):
            self.tenant = service.tenant

        def create(self, data, actor):
            calls.append((self.tenant, data.model_dump(), actor))
            return {"id": "request-1"}

        def change(self, request_id, **data):
            calls.append((self.tenant, request_id, data))
            return {"status": data["status"]}

    monkeypatch.setattr(api, "Requests", RequestsStub)
    from robothor.sales.tests.test_requests import request_data

    path = "/api/sales/requests"
    for headers in (
        {},
        {"x-test-role": "member"},
        {"x-test-role": "admin", "x-test-service": "yes"},
    ):
        assert c.post(path, headers=headers, json=request_data()).status_code == 403
    headers = {"x-test-role": "admin"}
    for extra in ({"actor": "forged"}, {"tenant_id": "other"}, {"sending_enabled": True}):
        assert c.post(path, headers=headers, json=request_data(**extra)).status_code == 422
    assert c.post(path, headers=headers, json=request_data()).status_code == 200
    assert calls.pop() == ("tenant-a", request_data(), "operator:user-1")
    request_id = "00000000-0000-4000-8000-000000000001"
    body = {"status": "paused", "expected_revision": 3, "reason": "Pause for business review"}
    assert c.post(f"{path}/{request_id}/state", headers=headers, json=body).status_code == 200
    assert calls.pop() == ("tenant-a", request_id, {**body, "actor": "operator:user-1"})
    assert (
        c.post(
            f"{path}/{request_id}/state", headers=headers, json={**body, "expected_revision": True}
        ).status_code
        == 422
    )


def test_preparation_recovery_never_accepts_service_or_caller_authority(client, monkeypatch):
    import robothor.sales.recovery as recovery

    c, calls = client
    monkeypatch.setattr(
        recovery.Recovery,
        "change",
        lambda self, prospect_id, **body: (
            calls.append((self.tenant, prospect_id, body)) or {"owner": "agent"}
        ),
    )
    path = "/api/sales/prospects/00000000-0000-4000-8000-000000000001/recovery"
    body = {
        "command": "resume",
        "expected_hash": "a" * 64,
        "reason": "Return the conversation to the agent",
    }
    for headers in (
        {},
        {"x-test-role": "member"},
        {"x-test-role": "admin", "x-test-service": "yes"},
    ):
        assert c.post(path, headers=headers, json=body).status_code == 403
    headers = {"x-test-role": "admin"}
    assert c.post(path, headers=headers, json={**body, "actor": "forged"}).status_code == 422
    assert c.post(path, headers=headers, json={**body, "command": "send"}).status_code == 422
    assert c.post(path, headers=headers, json=body).status_code == 200
    assert calls == [
        ("tenant-a", "00000000-0000-4000-8000-000000000001", {**body, "actor": "operator:user-1"})
    ]


def test_pipedrive_identity_api_requires_human_and_exact_packet(client, monkeypatch):
    import robothor.sales.pipedrive_identity as identity

    c, calls = client

    async def adopt(self, prospect_id, packet_id, expected_hash, actor, reason):
        calls.append((self.tenant, prospect_id, str(packet_id), expected_hash, actor))
        return {"organization_id": 11}

    monkeypatch.setattr(identity.IdentityReview, "adopt", adopt)
    path = "/api/sales/prospects/00000000-0000-4000-8000-000000000001/pipedrive/adopt"
    body = {
        "packet_id": "00000000-0000-4000-8000-000000000002",
        "expected_hash": "a" * 64,
        "reason": "Reviewed the provider records",
    }
    for headers in (
        {},
        {"x-test-role": "member"},
        {"x-test-role": "admin", "x-test-service": "yes"},
    ):
        assert c.post(path, headers=headers, json=body).status_code == 403
    headers = {"x-test-role": "admin"}
    assert c.post(path, headers=headers, json={**body, "organization_id": 99}).status_code == 422
    assert c.post(path, headers=headers, json=body).status_code == 200
    assert calls[0] == (
        "tenant-a",
        "00000000-0000-4000-8000-000000000001",
        body["packet_id"],
        body["expected_hash"],
        "operator:user-1",
    )
