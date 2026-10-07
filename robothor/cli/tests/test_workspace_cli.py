"""`genus workspace connect microsoft365` -- certificate, vault, settings, and the gate.

The vault is a dictionary here and the settings land in the contained
workspace's config.yaml; nothing reaches a database or a real tenant.
"""

from __future__ import annotations

import json
import os

import pytest
import yaml

from robothor.cli import main
from robothor.constants import DEFAULT_TENANT
from robothor.doctor.checks import workspace_m365
from robothor.workspace.microsoft import graph_client_from_vault
from robothor.workspace.tests.fake_graph import CLIENT_ID, TENANT_ID, FakeGraphTenant

ASSISTANT = "assistant@example.com"
OWNER = "owner@example.com"
CANARY = "canary@example.com"
PREFIX = "workspace/microsoft365/"

BASE_ARGS = [
    "workspace",
    "connect",
    "microsoft365",
    "--tenant-id",
    TENANT_ID,
    "--client-id",
    CLIENT_ID,
    "--assistant-mailbox",
    ASSISTANT,
    "--owner-mailbox",
    OWNER,
    "--canary-mailbox",
    CANARY,
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("ROBOTHOR_M365_", "ROBOTHOR_WORKSPACE_PROVIDER", "ROBOTHOR_TENANT")):
            monkeypatch.delenv(name, raising=False)
    from robothor.settings import reset_settings

    reset_settings()
    yield
    reset_settings()


class VaultRows(dict):
    """The vault as a dict, remembering every write's category and tenant."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[str, str, str]] = []


@pytest.fixture
def vault(monkeypatch) -> VaultRows:
    rows = VaultRows()

    def fake_get(key, *, tenant_id="default"):
        return rows.get(key)

    def fake_set(key, value, *, category="credential", tenant_id="default"):
        rows[key] = value
        rows.writes.append((key, category, tenant_id))

    monkeypatch.setattr("robothor.vault.get", fake_get)
    monkeypatch.setattr("robothor.vault.set", fake_set)
    return rows


def _config(workspace) -> dict:
    path = workspace / ".robothor" / "config.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _workspace_settings(workspace) -> dict:
    return (_config(workspace).get("settings") or {}).get("workspace") or {}


def test_dry_run_prints_permissions_and_powershell_and_writes_nothing(
    vault, env_workspace, capsys
) -> None:
    rc = main([*BASE_ARGS, "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0, out
    for permission in ("Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite"):
        assert permission in out
    assert "New-ManagementScope" in out
    assert "New-ManagementRoleAssignment" in out
    assert "New-ApplicationAccessPolicy" in out
    assert "PRIVATE KEY" not in out
    assert vault == {}
    assert _workspace_settings(env_workspace) == {}


def test_connect_stores_the_credential_and_the_settings(vault, env_workspace, capsys) -> None:
    rc = main(BASE_ARGS)
    out = capsys.readouterr().out
    assert rc == 0, out

    assert vault[PREFIX + "tenant_id"] == TENANT_ID
    assert vault[PREFIX + "client_id"] == CLIENT_ID
    assert "BEGIN CERTIFICATE" in vault[PREFIX + "client_certificate_pem"]
    key_pem = vault[PREFIX + "client_private_key_pem"]
    assert "PRIVATE KEY" in key_pem
    assert key_pem not in out and "PRIVATE KEY" not in out
    # The public certificate and its thumbprint are what the admin uploads.
    assert "BEGIN CERTIFICATE" in out
    from robothor.workspace.microsoft.connect import load_certificate

    cert = load_certificate(vault[PREFIX + "client_certificate_pem"], key_pem)
    assert cert.thumbprint_sha1 in out

    settings = _workspace_settings(env_workspace)
    assert settings["m365_assistant_mailbox"] == ASSISTANT
    assert settings["m365_owner_mailbox"] == OWNER
    assert settings["m365_scope_canary_mailbox"] == CANARY
    # Not enabled: the provider flips only with --enable and a passing probe.
    assert "workspace_provider" not in settings
    # Every row is a credential in the instance's own platform tenant.
    assert {category for _key, category, _tenant in vault.writes} == {"credential"}
    from robothor.settings import get_settings

    # Same resolution as the command: the configured tenant, else the default
    # tenant the Graph client reads from (graph_client_from_vault).
    expected_tenant = get_settings().database.tenant_id or DEFAULT_TENANT
    assert {tenant for _key, _category, tenant in vault.writes} == {expected_tenant}


def test_rerunning_reuses_the_certificate(vault, env_workspace, capsys) -> None:
    assert main(BASE_ARGS) == 0
    first_cert = vault[PREFIX + "client_certificate_pem"]
    first_key = vault[PREFIX + "client_private_key_pem"]
    capsys.readouterr()

    assert main(BASE_ARGS) == 0
    out = capsys.readouterr().out
    assert vault[PREFIX + "client_certificate_pem"] == first_cert
    assert vault[PREFIX + "client_private_key_pem"] == first_key
    assert "reusing" in out.lower()


def test_rotate_replaces_the_certificate(vault, env_workspace, capsys) -> None:
    assert main(BASE_ARGS) == 0
    first_cert = vault[PREFIX + "client_certificate_pem"]
    capsys.readouterr()

    assert main([*BASE_ARGS, "--rotate"]) == 0
    assert vault[PREFIX + "client_certificate_pem"] != first_cert


def test_dry_run_with_a_stored_certificate_shows_its_thumbprint(
    vault, env_workspace, capsys
) -> None:
    assert main(BASE_ARGS) == 0
    capsys.readouterr()
    from robothor.workspace.microsoft.connect import load_certificate

    cert = load_certificate(
        vault[PREFIX + "client_certificate_pem"], vault[PREFIX + "client_private_key_pem"]
    )
    snapshot = dict(vault)
    assert main([*BASE_ARGS, "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["certificate"]["thumbprint_sha1"] == cert.thumbprint_sha1
    assert payload["certificate"]["reused"] is True
    assert payload["dry_run"] is True
    assert "private_key_pem" not in json.dumps(payload)
    assert dict(vault) == snapshot


def test_json_output_carries_the_plan(vault, env_workspace, capsys) -> None:
    assert main([*BASE_ARGS, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["permissions"]["required"] == [
        "Mail.ReadWrite",
        "Mail.Send",
        "Calendars.ReadWrite",
    ]
    assert "New-ManagementScope" in payload["powershell"]["rbac_for_applications"]
    assert "New-ApplicationAccessPolicy" in payload["powershell"]["application_access_policy"]
    assert payload["certificate"]["certificate_pem"].startswith("-----BEGIN CERTIFICATE-----")
    assert payload["enabled"] is False
    assert "PRIVATE KEY" not in json.dumps(payload)


def test_bad_input_is_refused_before_anything_is_written(vault, env_workspace, capsys) -> None:
    args = list(BASE_ARGS)
    args[args.index(ASSISTANT)] = "o'brien@example.com"
    assert main(args) == 2
    assert vault == {}

    args = list(BASE_ARGS)
    args[args.index(CANARY)] = OWNER
    assert main(args) == 2
    assert "canary" in capsys.readouterr().err.lower()
    assert vault == {}

    args = list(BASE_ARGS)
    args[args.index(TENANT_ID)] = "common"
    assert main(args) == 2
    assert vault == {}


def test_a_first_connect_without_identifiers_is_refused(vault, env_workspace, capsys) -> None:
    assert main(["workspace", "connect", "microsoft365"]) == 2
    assert "--tenant-id" in capsys.readouterr().err
    assert vault == {}


def test_a_rerun_may_omit_what_is_already_stored(vault, env_workspace, capsys) -> None:
    assert main(BASE_ARGS) == 0
    capsys.readouterr()
    from robothor.settings import reset_settings

    reset_settings()
    assert main(["workspace", "connect", "microsoft365", "--dry-run"]) == 0
    assert TENANT_ID in capsys.readouterr().out


# ── --enable ─────────────────────────────────────────────────────────────────


@pytest.fixture
def tenant(vault, monkeypatch) -> FakeGraphTenant:
    """A tenant that trusts whatever certificate the command stored."""
    fake = FakeGraphTenant()
    fake.deny(CANARY)

    async def graph(tenant_id: str):
        fake.certificate_pem = vault.get(PREFIX + "client_certificate_pem")
        return await graph_client_from_vault(
            tenant_id,
            secret_get=lambda key, *, tenant_id: vault.get(key),
            transport=fake.transport(),
        )

    monkeypatch.setattr(workspace_m365, "_graph", graph)
    monkeypatch.setattr(workspace_m365, "_vault_connected", lambda _tenant: True)
    return fake


def test_enable_flips_the_provider_when_the_probe_passes(tenant, env_workspace, capsys) -> None:
    rc = main([*BASE_ARGS, "--enable"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert _workspace_settings(env_workspace)["workspace_provider"] == "microsoft365"
    assert "enabled" in out.lower()


def test_enable_refuses_when_the_canary_is_readable(tenant, env_workspace, capsys) -> None:
    tenant.denied_mailboxes.clear()
    rc = main([*BASE_ARGS, "--enable"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "app scope is not restricted" in captured.out + captured.err
    assert "workspace_provider" not in _workspace_settings(env_workspace)


def test_enable_refuses_without_a_canary(tenant, env_workspace, capsys) -> None:
    args = BASE_ARGS[: BASE_ARGS.index("--canary-mailbox")]
    rc = main([*args, "--enable"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "canary" in (captured.out + captured.err).lower()
    assert "workspace_provider" not in _workspace_settings(env_workspace)


def test_enable_refuses_when_the_app_cannot_read_the_assistant(
    tenant, env_workspace, capsys
) -> None:
    tenant.deny(ASSISTANT)
    assert main([*BASE_ARGS, "--enable"]) == 1
    assert "workspace_provider" not in _workspace_settings(env_workspace)


def test_enable_with_dry_run_is_refused(vault, env_workspace, capsys) -> None:
    assert main([*BASE_ARGS, "--enable", "--dry-run"]) == 2
    assert vault == {}
