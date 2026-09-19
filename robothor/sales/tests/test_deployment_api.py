"""Human identity reaches the real native deployment coordinator through HTTP."""

from unittest.mock import patch

import httpx
import pytest
from fastapi import APIRouter

from robothor.auth import tokens
from robothor.sales.tests.test_native_deployment import checkout as checkout
from robothor.sales.tests.test_native_deployment import native as native
from robothor.sales.tests.test_native_deployment import source as source

API = "/api/admin/sales-deployment"


@pytest.fixture
async def client(native, monkeypatch):
    from robothor.engine.health import create_health_app

    runtime, _, _, _ = native
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.setenv("ROBOTHOR_ENGINE_HOST", "0.0.0.0")
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "deployment-test-only-key-at-least-32-bytes")
    tokens.reset_signing_key_cache()
    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
    ):
        runtime.native.sales_runtime = runtime
        app = create_health_app(runtime.native.config, scheduler=runtime.native)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        yield client
    tokens.reset_signing_key_cache()


def headers(native, role="owner", tenant=None, service=False, user="human-test"):
    tenant = tenant or native[0].coordinator.sales.tenant
    token = (
        tokens.issue_service_token(
            "agent-test", tenant, audience="genus-engine", scopes=("engine:control",)
        )
        if service
        else tokens.issue_access_token(user, tenant, role, scopes=("engine:control",))
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller,expected", [("missing", 401), ("service", 403), ("member", 403), ("other-tenant", 403)]
)
async def test_controls_reject_unverified_or_nonoperator_identity(client, native, caller, expected):
    auth = (
        {}
        if caller == "missing"
        else headers(
            native,
            role="member" if caller == "member" else "owner",
            tenant="other" if caller == "other-tenant" else None,
            service=caller == "service",
        )
    )
    response = await client.post(
        API + "/prepare",
        headers=auth,
        json={
            "release_id": native[1],
            "expected_revision": native[0].coordinator.status()["settings_revision"],
            "reason": "Install reviewed release",
        },
    )
    assert response.status_code == expected
    assert native[0].coordinator.status()["pending"] is None


@pytest.mark.asyncio
async def test_inspect_prepare_commit_and_rollback_are_bound_to_human_and_exact_release(
    client, native
):
    runtime, release, _, _ = native
    auth = headers(native)
    inspected = await client.get(API + "/releases/" + release, headers=auth)
    assert inspected.status_code == 200
    assert inspected.json()["release_id"] == release
    assert inspected.json()["workflows"] == ["process"]
    assert "artifact_path" not in inspected.json()
    status = (await client.get(API, headers=auth)).json()
    prepared = await client.post(
        API + "/prepare",
        headers=auth,
        json={
            "release_id": release,
            "expected_revision": status["settings_revision"],
            "reason": "Install the reviewed release",
        },
    )
    assert prepared.status_code == 200, prepared.text
    record = prepared.json()
    pending = (await client.get(API, headers=auth)).json()
    assert pending["pending"]["id"] == record["id"]
    assert pending["runtime"]["ready"] is False
    committed = await client.post(
        API + "/transitions/" + record["id"] + "/commit",
        headers=headers(native, user="approver-test"),
        json={},
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["actor"] == "operator:human-test"
    with runtime.coordinator.sales.ops.transaction() as cur:
        cur.execute(
            "SELECT actor FROM operation_audit WHERE tenant_id=%s AND entity_id=%s AND event='sales.deployment.committed'",
            (runtime.coordinator.sales.tenant, record["id"]),
        )
        assert cur.fetchone()["actor"] == "operator:approver-test"
    status = (await client.get(API, headers=auth)).json()
    assert status["selected_release_id"] == release and status["runtime"]["ready"] is True
    assert status["pending"] is None
    assert runtime.coordinator.sales.settings()["sending_enabled"] is False
    rollback = await client.post(
        API + "/transitions/" + record["id"] + "/rollback",
        headers=auth,
        json={
            "expected_revision": status["settings_revision"],
            "reason": "Restore the previous fleet selection",
        },
    )
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["direction"] == "rollback"
    assert runtime.coordinator.sales.settings()["fleet_release_id"] == release
    aborted = await client.post(
        API + "/transitions/" + rollback.json()["id"] + "/abort",
        headers=auth,
        json={"reason": "Keep the installed release after review"},
    )
    assert aborted.status_code == 200, aborted.text
    assert aborted.json()["status"] == "aborted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"actor": "operator:forged"},
        {"tenant_id": "other"},
        {"runtime_evidence": {}},
        {"sending_enabled": True},
    ],
)
async def test_request_cannot_choose_identity_proof_or_activation_switches(client, native, extra):
    body = {
        "release_id": native[1],
        "expected_revision": native[0].coordinator.status()["settings_revision"],
        "reason": "Install reviewed release",
        **extra,
    }
    response = await client.post(API + "/prepare", headers=headers(native), json=body)
    assert response.status_code == 422
    assert native[0].coordinator.status()["pending"] is None


@pytest.mark.asyncio
async def test_stale_preparation_and_foreign_transition_do_not_mutate(client, native):
    response = await client.post(
        API + "/prepare",
        headers=headers(native),
        json={
            "release_id": native[1],
            "expected_revision": 999,
            "reason": "Install reviewed release",
        },
    )
    assert response.status_code == 409
    response = await client.post(
        API + "/transitions/00000000-0000-0000-0000-000000000001/commit",
        headers=headers(native),
        json={},
    )
    assert response.status_code == 409
    assert native[0].coordinator.status()["pending"] is None


@pytest.mark.asyncio
async def test_commit_cannot_accept_a_client_readiness_proof(client, native):
    response = await client.post(
        API + "/transitions/00000000-0000-0000-0000-000000000001/commit",
        headers=headers(native),
        json={"runtime_evidence": {"ready": True}},
    )
    assert response.status_code == 422
    assert native[0].coordinator.status()["pending"] is None
