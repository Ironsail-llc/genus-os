"""Private RPC authenticates before parsing and never echoes sensitive input."""

from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from robothor.auth import tokens
from robothor.autonomy.workflows.api import create_app
from robothor.autonomy.workflows.client import invoke
from robothor.autonomy.workflows.protocol import AUDIENCE, SCOPE


@pytest.fixture
def signing(monkeypatch):
    monkeypatch.setattr(tokens, "signing_key", lambda: "fixture-only-signing-key-" * 3)


async def test_authentication_precedes_validation(signing, identity):
    manager = AsyncMock()
    transport = httpx.ASGITransport(app=create_app(manager))
    bad_tokens = [
        "",
        tokens.issue_service_token(identity.owner_id, identity.tenant_id),
        tokens.issue_access_token(
            identity.owner_id, identity.tenant_id, "owner", audience=AUDIENCE, scopes=(SCOPE,)
        ),
        tokens.issue_service_token(
            identity.owner_id, identity.tenant_id, audience=AUDIENCE, scopes=("bridge:read",)
        ),
    ]
    async with httpx.AsyncClient(transport=transport, base_url="http://autonomy") as client:
        for bearer in bad_tokens:
            response = await client.post(
                "/rpc",
                content=b"sensitive-invalid-body",
                headers={"Authorization": "Bearer " + bearer},
            )
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
            assert "sensitive" not in response.text
    manager.open.assert_not_called()
    manager.execute.assert_not_called()


async def test_signed_identity_cannot_be_overridden_by_body(signing, identity):
    manager = AsyncMock()
    manager.inspect.return_value = {"revision": 1}
    transport = httpx.ASGITransport(app=create_app(manager))
    workflow = str(uuid4())
    assert await invoke(
        identity, "main", {"kind": "inspect", "workflow_id": workflow}, transport=transport
    ) == {"revision": 1}
    manager.inspect.assert_awaited_once_with(identity, "main", workflow)
    bearer = tokens.issue_service_token(
        identity.owner_id, identity.tenant_id, agent_id="main", audience=AUDIENCE, scopes=(SCOPE,)
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://autonomy") as client:
        for extra in ({"owner_id": "another-owner"}, {"script": "sensitive-script"}):
            response = await client.post(
                "/rpc",
                json={"kind": "inspect", "workflow_id": workflow, **extra},
                headers={"Authorization": "Bearer " + bearer},
            )
            assert response.status_code == 422
            assert response.json() == {"error": "invalid_workflow_request"}
    assert manager.inspect.await_count == 1


async def test_private_errors_do_not_cross_rpc_boundary(signing, identity):
    manager = AsyncMock()
    manager.inspect.side_effect = RuntimeError("private-browser-value")
    result = await invoke(
        identity,
        "main",
        {"kind": "inspect", "workflow_id": str(uuid4())},
        transport=httpx.ASGITransport(app=create_app(manager)),
    )
    assert result == {"error": "workflow_request_failed"}
