"""Building the Graph client from the vault, and the workspace settings."""

from __future__ import annotations

import pytest

from robothor.workspace.errors import AuthError
from robothor.workspace.microsoft import graph_client_from_vault
from robothor.workspace.tests.fake_graph import (
    CLIENT_ID,
    TENANT_ID,
    FakeGraphTenant,
    make_certificate,
)


class FakeVault:
    def __init__(self, rows: dict[str, str]) -> None:
        self.rows = rows
        self.reads: list[tuple[str, str]] = []

    def __call__(self, key: str, *, tenant_id: str) -> str | None:
        self.reads.append((key, tenant_id))
        return self.rows.get(key)


async def test_missing_config_is_not_connected() -> None:
    with pytest.raises(AuthError, match="microsoft365 not connected"):
        await graph_client_from_vault("acme", secret_get=FakeVault({}))


async def test_tenant_and_client_without_any_credential_is_not_connected() -> None:
    vault = FakeVault(
        {
            "workspace/microsoft365/tenant_id": TENANT_ID,
            "workspace/microsoft365/client_id": CLIENT_ID,
        }
    )
    with pytest.raises(AuthError, match="microsoft365 not connected"):
        await graph_client_from_vault("acme", secret_get=vault)


async def test_certificate_from_vault_reaches_graph() -> None:
    cert_pem, key_pem = make_certificate()
    tenant = FakeGraphTenant(certificate_pem=cert_pem)
    tenant.mailbox("assistant@example.com").messages.append({"id": "m1"})
    vault = FakeVault(
        {
            "workspace/microsoft365/tenant_id": TENANT_ID,
            "workspace/microsoft365/client_id": CLIENT_ID,
            "workspace/microsoft365/client_certificate_pem": cert_pem,
            "workspace/microsoft365/client_private_key_pem": key_pem,
        }
    )
    graph = await graph_client_from_vault("acme", secret_get=vault, transport=tenant.transport())
    try:
        result = await graph.get("/users/assistant@example.com/messages")
    finally:
        await graph.aclose()
    assert result["value"] == [{"id": "m1"}]
    assert "client_assertion" in tenant.token_requests[-1]
    # Every read is scoped to the platform tenant it was asked for.
    assert {t for _, t in vault.reads} == {"acme"}


async def test_secret_is_reread_from_the_vault_on_refresh() -> None:
    tenant = FakeGraphTenant(client_secret="first")
    vault = FakeVault(
        {
            "workspace/microsoft365/tenant_id": TENANT_ID,
            "workspace/microsoft365/client_id": CLIENT_ID,
            "workspace/microsoft365/client_secret": "first",
        }
    )
    graph = await graph_client_from_vault("acme", secret_get=vault, transport=tenant.transport())
    try:
        await graph.get("/users/a@example.com/messages")
        vault.rows["workspace/microsoft365/client_secret"] = "second"
        tenant.client_secret = "second"
        graph.token_source.forget()  # type: ignore[attr-defined]
        await graph.get("/users/a@example.com/messages")
    finally:
        await graph.aclose()
    assert tenant.token_requests[-1]["client_secret"] == "second"


def test_workspace_settings_defaults(monkeypatch) -> None:
    from robothor.settings import get_settings, reset_settings

    for name in (
        "ROBOTHOR_WORKSPACE_PROVIDER",
        "ROBOTHOR_M365_ASSISTANT_MAILBOX",
        "ROBOTHOR_M365_OWNER_MAILBOX",
        "ROBOTHOR_M365_SCOPE_CANARY_MAILBOX",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    try:
        settings = get_settings().workspace
        assert settings.workspace_provider == "google"
        assert settings.m365_assistant_mailbox == ""
        assert settings.m365_owner_mailbox == ""
        assert settings.m365_scope_canary_mailbox == ""
    finally:
        reset_settings()


def test_workspace_provider_reads_its_env_and_rejects_unknown(monkeypatch) -> None:
    from pydantic import ValidationError

    from robothor.settings import get_settings, reset_settings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE_PROVIDER", "microsoft365")
    monkeypatch.setenv("ROBOTHOR_M365_ASSISTANT_MAILBOX", "assistant@example.com")
    reset_settings()
    try:
        settings = get_settings().workspace
        assert settings.workspace_provider == "microsoft365"
        assert settings.m365_assistant_mailbox == "assistant@example.com"
        monkeypatch.setenv("ROBOTHOR_WORKSPACE_PROVIDER", "exchange2003")
        reset_settings()
        with pytest.raises(ValidationError):
            get_settings()
    finally:
        reset_settings()
