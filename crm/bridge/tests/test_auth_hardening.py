"""Fail-closed bridge authentication, tenant binding, scopes, and agent identity."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.auth import tokens


@pytest.fixture(autouse=True)
def auth_key(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


def _secure_mode(monkeypatch) -> None:
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.delenv("GENUS_AUTH_ENFORCE", raising=False)
    monkeypatch.setenv("ROBOTHOR_BRIDGE_HOST", "0.0.0.0")


@pytest.mark.asyncio
async def test_missing_token_is_denied_by_default(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    with patch("routers.people.list_people") as list_people:
        response = await test_client.get("/api/people")
    assert response.status_code == 401
    assert response.json() == {"error": "authentication required"}
    list_people.assert_not_called()


@pytest.mark.asyncio
async def test_liveness_alias_is_public_but_detailed_health_is_private(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    live = await test_client.get("/live")
    detailed = await test_client.get("/health")
    assert live.status_code == 200
    assert detailed.status_code == 401


@pytest.mark.asyncio
async def test_forged_tenant_header_is_rejected(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    token = tokens.issue_access_token("user-1", "tenant-a", "member")
    with patch("routers.people.list_people") as list_people:
        response = await test_client.get(
            "/api/people",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Tenant-Id": "tenant-b",
            },
        )
    assert response.status_code == 403
    assert response.json() == {"error": "tenant not authorized"}
    list_people.assert_not_called()


@pytest.mark.asyncio
async def test_matching_tenant_header_uses_verified_tenant(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    token = tokens.issue_access_token("user-1", "tenant-a", "member")
    with patch("routers.people.list_people", return_value=[]) as list_people:
        response = await test_client.get(
            "/api/people",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Tenant-Id": "tenant-a",
            },
        )
    assert response.status_code == 200
    assert list_people.call_args.kwargs["tenant_id"] == "tenant-a"
    assert response.headers["x-tenant-id"] == "tenant-a"


@pytest.mark.asyncio
async def test_forged_agent_header_is_rejected(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    token = tokens.issue_service_token(
        "bridge-client",
        "tenant-a",
        agent_id="email-classifier",
    )
    response = await test_client.get(
        "/api/conversations",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Agent-Id": "crm-steward",
        },
    )
    assert response.status_code == 403
    assert response.json() == {"error": "agent identity not authorized"}


@pytest.mark.asyncio
async def test_verified_service_agent_drives_authorship(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    token = tokens.issue_service_token(
        "bridge-client",
        "tenant-a",
        agent_id="email-classifier",
    )
    with (
        patch("routers.notes_tasks.create_task", return_value="task-1") as create_task,
        patch("routers.notes_tasks.publish"),
    ):
        response = await test_client.post(
            "/api/tasks",
            json={"title": "Verified task"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert create_task.call_args.kwargs["tenant_id"] == "tenant-a"
    assert create_task.call_args.kwargs["created_by_agent"] == "email-classifier"


@pytest.mark.asyncio
async def test_wrong_audience_is_rejected(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    token = tokens.issue_access_token(
        "user-1",
        "tenant-a",
        "member",
        audience="genus-engine",
    )
    response = await test_client.get("/api/people", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json() == {"error": "invalid or expired token"}


@pytest.mark.asyncio
async def test_write_requires_write_scope(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    token = tokens.issue_access_token(
        "user-1",
        "tenant-a",
        "member",
        scopes=("bridge:read",),
    )
    response = await test_client.post(
        "/api/people",
        json={"firstName": "Ada"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    assert response.json() == {"error": "insufficient scope"}


@pytest.mark.asyncio
async def test_tenant_admin_route_requires_authorized_role(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    token = tokens.issue_access_token(
        "user-1",
        "tenant-a",
        "member",
        scopes=("bridge:*", "tenant:admin"),
    )
    response = await test_client.get("/api/tenants", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json() == {"error": "role not authorized"}


@pytest.mark.asyncio
async def test_explicit_loopback_dev_mode_preserves_legacy_headers(test_client):
    with patch("routers.people.list_people", return_value=[]) as list_people:
        response = await test_client.get(
            "/api/people",
            headers={"X-Tenant-Id": "legacy-tenant", "X-Agent-Id": "crm-steward"},
        )
    assert response.status_code == 200
    assert list_people.call_args.kwargs["tenant_id"] == "legacy-tenant"


@pytest.mark.asyncio
async def test_production_sso_rejects_unconfigured_issuer(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    monkeypatch.setenv("GENUS_ENVIRONMENT", "production")
    monkeypatch.setenv("GENUS_BRIDGE_SSO_SECRET", "dashboard-secret")
    monkeypatch.setenv("GENUS_OIDC_ISSUERS", "https://trusted.example")
    response = await test_client.post(
        "/api/auth/sso",
        json={
            "issuer": "https://evil.example",
            "subject": "subject-1",
            "email": "user@example.com",
            "email_verified": True,
        },
        headers={"X-Bridge-Auth": "dashboard-secret"},
    )
    assert response.status_code == 403
    assert response.json() == {"error": "identity provider not authorized"}


@pytest.mark.asyncio
async def test_default_member_cannot_list_or_retrieve_vault_secrets(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    token = tokens.issue_access_token("member-1", "tenant-a", "member")

    with (
        patch("robothor.vault.list") as vault_list,
        patch("robothor.vault.get") as vault_get,
    ):
        listed = await test_client.get(
            "/api/vault/list",
            headers={"Authorization": f"Bearer {token}"},
        )
        retrieved = await test_client.get(
            "/api/vault/get?key=payments/provider-token",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert listed.status_code == 403
    assert retrieved.status_code == 403
    assert listed.json() == {"error": "vault access not authorized"}
    assert retrieved.json() == {"error": "vault access not authorized"}
    vault_list.assert_not_called()
    vault_get.assert_not_called()


@pytest.mark.asyncio
async def test_admin_vault_access_is_bound_to_verified_tenant(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    token = tokens.issue_access_token("admin-1", "tenant-a", "admin")

    with patch("robothor.vault.get", return_value="opaque-secret") as vault_get:
        response = await test_client.get(
            "/api/vault/get?key=payments/provider-token",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "key": "payments/provider-token",
        "value": "opaque-secret",
    }
    vault_get.assert_called_once_with("payments/provider-token", tenant_id="tenant-a")


@pytest.mark.asyncio
async def test_integration_routes_require_narrow_permission(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    member_token = tokens.issue_access_token("member-1", "tenant-a", "member")
    integration_token = tokens.issue_access_token(
        "member-2",
        "tenant-a",
        "member",
        scopes=("bridge:write", "integration:write"),
    )
    resolved = {
        "person_id": "person-1",
        "channel": "email",
        "identifier": "ada@example.com",
    }

    with patch("robothor.crm.dal.resolve_contact", return_value=resolved) as resolve:
        denied = await test_client.post(
            "/resolve-contact",
            json={"channel": "email", "identifier": "ada@example.com"},
            headers={"Authorization": f"Bearer {member_token}"},
        )
        allowed = await test_client.post(
            "/resolve-contact",
            json={"channel": "email", "identifier": "ada@example.com"},
            headers={"Authorization": f"Bearer {integration_token}"},
        )

    assert denied.status_code == 403
    assert denied.json() == {"error": "integration access not authorized"}
    assert allowed.status_code == 200
    resolve.assert_called_once_with(
        "email",
        "ada@example.com",
        None,
        tenant_id="tenant-a",
    )


@pytest.mark.asyncio
async def test_integration_timeline_removes_cross_tenant_mappings(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    token = tokens.issue_access_token("admin-1", "tenant-a", "admin")
    timeline = {
        "identifier": "ada@example.com",
        "mappings": [
            {"tenant_id": "tenant-a", "identifier": "ada@example.com"},
            {"tenant_id": "tenant-b", "identifier": "ada@example.com"},
        ],
        "conversations": [],
    }

    with patch("robothor.crm.dal.get_timeline", return_value=timeline) as get_timeline:
        response = await test_client.get(
            "/timeline/ada@example.com",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["mappings"] == [timeline["mappings"][0]]
    get_timeline.assert_called_once_with("ada@example.com", tenant_id="tenant-a")


@pytest.mark.asyncio
async def test_installed_agent_administration_denies_member_and_allows_admin(
    test_client, monkeypatch
):
    from robothor.constants import DEFAULT_TENANT

    _secure_mode(monkeypatch)
    member_token = tokens.issue_access_token("member-1", DEFAULT_TENANT, "member")
    admin_token = tokens.issue_access_token("admin-1", DEFAULT_TENANT, "admin")
    config = MagicMock()
    config.installed_agents = {}

    with patch("robothor.templates.instance.InstanceConfig.load", return_value=config) as load:
        denied = await test_client.get(
            "/api/installed-agents",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        allowed = await test_client.get(
            "/api/installed-agents",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert denied.status_code == 403
    assert denied.json() == {"error": "agent administration not authorized"}
    assert allowed.status_code == 200
    assert allowed.json() == {"agents": [], "count": 0}
    load.assert_called_once()


@pytest.mark.asyncio
async def test_installed_agent_mutation_requires_admin_permission(test_client, monkeypatch):
    from robothor.constants import DEFAULT_TENANT

    _secure_mode(monkeypatch)
    member_token = tokens.issue_access_token("member-1", DEFAULT_TENANT, "member")
    admin_token = tokens.issue_access_token("admin-1", DEFAULT_TENANT, "admin")
    hub_client = MagicMock()
    hub_client.__enter__.return_value = hub_client
    hub_client.get_bundle.return_value = {
        "slug": "example-agent",
        "sha256": "a" * 64,
    }
    hub_client.download_bundle.return_value = {"manifest": {"id": "example-agent"}}

    with (
        patch("robothor.templates.hub_client.HubClient", return_value=hub_client),
        patch(
            "robothor.templates.installer.install",
            return_value={"agent_id": "example-agent", "files_created": []},
        ) as install,
    ):
        denied = await test_client.post(
            "/api/installed-agents/install",
            json={"slug": "example-agent"},
            headers={"Authorization": f"Bearer {member_token}"},
        )
        allowed = await test_client.post(
            "/api/installed-agents/install",
            json={"slug": "example-agent"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert denied.status_code == 403
    assert denied.json() == {"error": "agent administration not authorized"}
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "installed"
    hub_client.download_bundle.assert_called_once_with("example-agent", expected_sha256="a" * 64)
    install.assert_called_once_with(
        {"manifest": {"id": "example-agent"}},
        overrides={},
        auto_yes=True,
        source="hub",
        source_ref="example-agent",
        source_sha256="a" * 64,
    )


@pytest.mark.asyncio
async def test_installed_agent_update_is_integrity_pinned(test_client, monkeypatch):
    from robothor.constants import DEFAULT_TENANT

    _secure_mode(monkeypatch)
    admin_token = tokens.issue_access_token("admin-1", DEFAULT_TENANT, "admin")
    hub_client = MagicMock()
    hub_client.__enter__.return_value = hub_client
    hub_client.get_bundle.return_value = {
        "slug": "example-agent",
        "sha256": "b" * 64,
    }
    hub_client.download_bundle.return_value = "/tmp/verified-example-agent"

    with (
        patch("robothor.templates.hub_client.HubClient", return_value=hub_client),
        patch(
            "robothor.templates.installer.update",
            return_value={"agent_id": "example-agent", "version": "2.0.0"},
        ) as update,
    ):
        response = await test_client.post(
            "/api/installed-agents/example-agent/update",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert response.status_code == 200
    assert response.json()["new_version"] == "2.0.0"
    hub_client.download_bundle.assert_called_once_with("example-agent", expected_sha256="b" * 64)
    update.assert_called_once_with(
        "example-agent",
        new_template_path="/tmp/verified-example-agent",
        auto_yes=True,
        source="hub",
        source_ref="example-agent",
        source_sha256="b" * 64,
    )


@pytest.mark.asyncio
async def test_secondary_tenant_cannot_administer_appliance_agents(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    token = tokens.issue_access_token("admin-1", "tenant-b", "admin")

    with patch("robothor.templates.instance.InstanceConfig.load") as load:
        response = await test_client.get(
            "/api/installed-agents",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json() == {"error": "appliance administration not authorized for tenant"}
    load.assert_not_called()


@pytest.mark.asyncio
async def test_memory_blocks_are_privileged_and_tenant_bound(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    member_token = tokens.issue_access_token("member-1", "tenant-a", "member")
    admin_token = tokens.issue_access_token("admin-1", "tenant-a", "admin")

    with patch(
        "robothor.memory.blocks.list_blocks",
        return_value={"blocks": []},
    ) as list_blocks:
        denied = await test_client.get(
            "/api/memory/blocks",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        allowed = await test_client.get(
            "/api/memory/blocks",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert denied.status_code == 403
    assert denied.json() == {"error": "memory administration not authorized"}
    assert allowed.status_code == 200
    list_blocks.assert_called_once_with(tenant_id="tenant-a")


@pytest.mark.asyncio
async def test_memory_ingestion_requires_permission_and_primary_tenant(test_client, monkeypatch):
    from robothor.constants import DEFAULT_TENANT

    _secure_mode(monkeypatch)
    member_token = tokens.issue_access_token("member-1", DEFAULT_TENANT, "member")
    admin_token = tokens.issue_access_token("admin-1", DEFAULT_TENANT, "admin")
    secondary_admin_token = tokens.issue_access_token("admin-2", "tenant-b", "admin")

    with patch(
        "robothor.memory.ingestion.ingest_content",
        new_callable=AsyncMock,
        return_value={"stored_ids": [1]},
    ) as ingest:
        denied_member = await test_client.post(
            "/api/memory/store",
            json={"content": "a durable fact"},
            headers={"Authorization": f"Bearer {member_token}"},
        )
        denied_tenant = await test_client.post(
            "/api/memory/store",
            json={"content": "a durable fact"},
            headers={"Authorization": f"Bearer {secondary_admin_token}"},
        )
        allowed = await test_client.post(
            "/api/memory/store",
            json={"content": "a durable fact"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert denied_member.status_code == 403
    assert denied_member.json() == {"error": "memory administration not authorized"}
    assert denied_tenant.status_code == 403
    assert denied_tenant.json() == {"error": "memory operation not authorized for tenant"}
    assert allowed.status_code == 200
    ingest.assert_awaited_once_with(
        "a durable fact",
        source_channel="api",
        content_type="conversation",
        metadata={"tenant_id": DEFAULT_TENANT},
    )


@pytest.mark.asyncio
async def test_member_cannot_approve_or_reject_tasks(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    token = tokens.issue_access_token("member-1", "tenant-a", "member")

    with (
        patch("routers.notes_tasks.approve_task") as approve,
        patch("routers.notes_tasks.reject_task") as reject,
    ):
        approve_response = await test_client.post(
            "/api/tasks/task-1/approve",
            json={"resolution": "approved"},
            headers={"Authorization": f"Bearer {token}"},
        )
        reject_response = await test_client.post(
            "/api/tasks/task-1/reject",
            json={"reason": "rework"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert approve_response.status_code == 403
    assert reject_response.status_code == 403
    assert approve_response.json() == {"error": "task approval not authorized"}
    approve.assert_not_called()
    reject.assert_not_called()


@pytest.mark.asyncio
async def test_admin_can_approve_tenant_task(test_client, monkeypatch):
    _secure_mode(monkeypatch)
    token = tokens.issue_access_token("admin-1", "tenant-a", "admin")

    with (
        patch("routers.notes_tasks.approve_task", return_value=True) as approve,
        patch("routers.notes_tasks.publish"),
    ):
        response = await test_client.post(
            "/api/tasks/task-1/approve",
            json={"resolution": "approved"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    approve.assert_called_once_with(
        "task-1",
        "approved",
        "admin-1",
        tenant_id="tenant-a",
    )


@pytest.mark.asyncio
async def test_agent_status_uses_verified_tenant(test_client, monkeypatch):
    import routers.agents as agents_router

    _secure_mode(monkeypatch)
    token = tokens.issue_access_token("member-1", "tenant-a", "member")
    agents_router._cache = {"data": None, "expires": 0.0, "tenant_id": None}
    result = {
        "agents": [],
        "summary": {
            "healthy": 0,
            "degraded": 0,
            "failed": 0,
            "sleeping": 0,
            "unknown": 0,
            "total": 0,
        },
    }

    with patch.object(agents_router, "_build_agent_status", return_value=result) as build:
        response = await test_client.get(
            "/api/agents/status",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    build.assert_called_once_with(tenant_id="tenant-a")
