"""The Microsoft 365 doctor checks, against an in-memory tenant.

Every outcome an operator can meet is pinned here: not connected, a refused
credential, a mailbox the app cannot read, and -- the one this check exists
for -- a canary mailbox the app CAN read, which means the admin granted the
app the whole company's mail.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.doctor.checks import workspace_m365
from robothor.doctor.tests.conftest import make_ctx
from robothor.workspace.errors import AuthError
from robothor.workspace.microsoft import graph_client_from_vault
from robothor.workspace.tests.fake_graph import (
    CLIENT_ID,
    TENANT_ID,
    FakeGraphTenant,
    make_certificate,
)

ASSISTANT = "assistant@example.com"
OWNER = "owner@example.com"
CANARY = "canary@example.com"

_BY_ID = {check.id: check for check in workspace_m365.CHECKS}


def _run(check_id: str, ctx=None) -> list:
    answer = asyncio.run(_BY_ID[check_id].run(ctx or make_ctx()))
    return answer if isinstance(answer, list) else [answer]


def _by_sub(rows: list) -> dict:
    return {row.sub_id: row for row in rows}


@pytest.fixture
def settings(monkeypatch):
    def _set(**pairs: str) -> None:
        from robothor.settings import reset_settings

        for name, value in pairs.items():
            monkeypatch.setenv(name, value)
        reset_settings()

    return _set


@pytest.fixture
def tenant(monkeypatch) -> FakeGraphTenant:
    """A scoped tenant: the canary answers 403, the vault holds a certificate."""
    cert_pem, key_pem = make_certificate()
    fake = FakeGraphTenant(certificate_pem=cert_pem)
    fake.deny(CANARY)
    rows = {
        "workspace/microsoft365/tenant_id": TENANT_ID,
        "workspace/microsoft365/client_id": CLIENT_ID,
        "workspace/microsoft365/client_certificate_pem": cert_pem,
        "workspace/microsoft365/client_private_key_pem": key_pem,
    }

    def secret_get(key: str, *, tenant_id: str) -> str | None:
        return rows.get(key)

    async def graph(tenant_id: str):
        return await graph_client_from_vault(
            tenant_id, secret_get=secret_get, transport=fake.transport()
        )

    monkeypatch.setattr(workspace_m365, "_graph", graph)
    monkeypatch.setattr(workspace_m365, "_vault_connected", lambda _tenant: True)
    fake.vault_rows = rows  # type: ignore[attr-defined]
    return fake


@pytest.fixture
def connected(settings, tenant) -> FakeGraphTenant:
    settings(
        ROBOTHOR_WORKSPACE_PROVIDER="microsoft365",
        ROBOTHOR_M365_ASSISTANT_MAILBOX=ASSISTANT,
        ROBOTHOR_M365_OWNER_MAILBOX=OWNER,
        ROBOTHOR_M365_SCOPE_CANARY_MAILBOX=CANARY,
        ROBOTHOR_TIMEZONE="America/New_York",
    )
    tenant.mailbox(OWNER).time_zone = "Eastern Standard Time"
    return tenant


# ── registration ─────────────────────────────────────────────────────────────


def test_the_checks_are_built_in_with_the_documented_severities() -> None:
    from robothor.doctor.registry import builtin_checks

    by_id = {check.id: check for check in builtin_checks()}
    assert by_id["workspace.m365_connection"].severity == "required"
    assert by_id["workspace.m365_scope"].severity == "required"
    assert by_id["workspace.m365_canary_configured"].severity == "recommended"
    assert by_id["workspace.m365_timezone"].severity == "recommended"


# ── inactive on a Google instance ────────────────────────────────────────────


def test_a_google_instance_with_no_m365_credential_skips_everything(monkeypatch) -> None:
    monkeypatch.setattr(workspace_m365, "_vault_connected", lambda _tenant: False)

    async def boom(_tenant):  # pragma: no cover - must not be reached
        raise AssertionError("a Google instance must not build a Graph client")

    monkeypatch.setattr(workspace_m365, "_graph", boom)
    for check_id in _BY_ID:
        rows = _run(check_id)
        assert all(row.status == "skip" for row in rows), (check_id, rows)
        assert "google" in rows[0].detail


def test_a_vault_that_cannot_be_read_counts_as_not_connected(monkeypatch) -> None:
    def broken(_tenant):
        raise RuntimeError("no database")

    from robothor import vault

    monkeypatch.setattr(vault, "get", lambda *a, **k: broken(None))
    assert workspace_m365._vault_connected("default") is False


def test_stored_credentials_activate_the_checks_on_a_google_instance(settings, tenant) -> None:
    """Connected but not yet enabled is exactly when the operator runs the doctor."""
    settings(ROBOTHOR_M365_ASSISTANT_MAILBOX=ASSISTANT, ROBOTHOR_M365_OWNER_MAILBOX=OWNER)
    rows = _by_sub(_run("workspace.m365_connection"))
    assert rows["token"].status == "pass"


# ── connection ───────────────────────────────────────────────────────────────


def test_a_scoped_tenant_passes_every_step(connected) -> None:
    rows = _by_sub(_run("workspace.m365_connection"))
    assert {sub: row.status for sub, row in rows.items()} == {
        "config": "pass",
        "token": "pass",
        "assistant_inbox": "pass",
        "owner_calendar": "pass",
    }
    inbox = connected.requests_matching(
        "GET", "/users/assistant@example.com/mailFolders/inbox/messages"
    )
    assert inbox, [str(r.url) for r in connected.requests]
    assert inbox[0].url.params["$top"] == "1"
    assert inbox[0].url.params["$select"] == "id"


def test_missing_mailboxes_fail_config_and_skip_the_rest(settings, tenant) -> None:
    settings(ROBOTHOR_WORKSPACE_PROVIDER="microsoft365")
    rows = _by_sub(_run("workspace.m365_connection"))
    assert rows["config"].status == "fail"
    assert "ROBOTHOR_M365_ASSISTANT_MAILBOX" in rows["config"].detail
    assert "genus workspace connect microsoft365" in rows["config"].detail
    assert rows["token"].status == "skip"


def test_no_credential_in_the_vault_fails_config(connected, monkeypatch) -> None:
    async def not_connected(_tenant):
        raise AuthError("microsoft365 not connected")

    monkeypatch.setattr(workspace_m365, "_graph", not_connected)
    rows = _by_sub(_run("workspace.m365_connection"))
    assert rows["config"].status == "fail"
    assert "not connected" in rows["config"].detail


def test_a_refused_certificate_fails_the_token_step(connected) -> None:
    other_cert, _ = make_certificate("someone-else")
    connected.certificate_pem = other_cert
    rows = _by_sub(_run("workspace.m365_connection"))
    assert rows["config"].status == "pass"
    assert rows["token"].status == "fail"
    assert "AADSTS700027" in rows["token"].detail
    assert "BEGIN" not in rows["token"].detail
    assert rows["assistant_inbox"].status == "skip"


def test_an_unreadable_assistant_inbox_fails_and_names_the_scope(connected) -> None:
    connected.deny(ASSISTANT)
    rows = _by_sub(_run("workspace.m365_connection"))
    assert rows["assistant_inbox"].status == "fail"
    assert "403" in rows["assistant_inbox"].detail
    assert ASSISTANT in rows["assistant_inbox"].detail
    assert "Mail.ReadWrite" in rows["assistant_inbox"].detail


def test_an_unreadable_owner_calendar_fails(connected) -> None:
    connected.deny(OWNER)
    rows = _by_sub(_run("workspace.m365_connection"))
    assert rows["assistant_inbox"].status == "pass"
    assert rows["owner_calendar"].status == "fail"
    assert "Calendars.ReadWrite" in rows["owner_calendar"].detail


def test_offline_reads_config_and_leaves_the_box_alone(connected) -> None:
    rows = _by_sub(_run("workspace.m365_connection", make_ctx(offline=True)))
    assert rows["config"].status == "pass"
    assert rows["token"].status == "skip"
    assert connected.token_requests == []
    assert connected.requests == []


def test_no_token_ever_reaches_a_result(connected) -> None:
    rows = _run("workspace.m365_connection")
    rows += _run("workspace.m365_scope")
    for row in rows:
        for token in connected.issued_tokens:
            assert token not in row.detail


# ── scope (the canary) ───────────────────────────────────────────────────────


def test_a_denied_canary_proves_the_scope(connected) -> None:
    [row] = _run("workspace.m365_scope")
    assert row.status == "pass", row.detail
    assert CANARY in row.detail


def test_a_readable_canary_is_an_error(connected) -> None:
    connected.denied_mailboxes.clear()
    [row] = _run("workspace.m365_scope")
    assert row.status == "fail"
    assert "app scope is not restricted" in row.detail
    assert "beyond assistant+owner" in row.detail


def test_a_missing_canary_mailbox_is_not_proof(connected) -> None:
    connected.denied_mailboxes.clear()
    connected.fail("GET", r"/users/[^/]+/mailFolders/inbox/messages", 404)
    [row] = _run("workspace.m365_scope")
    assert row.status == "fail"
    assert "not found" in row.detail


def test_a_canary_that_is_the_assistant_is_refused(connected, settings) -> None:
    settings(ROBOTHOR_M365_SCOPE_CANARY_MAILBOX=ASSISTANT.upper())
    [row] = _run("workspace.m365_scope")
    assert row.status == "fail"
    assert "must be a third mailbox" in row.detail


def test_no_canary_skips_the_scope_and_warns_in_its_own_check(connected, settings) -> None:
    settings(ROBOTHOR_M365_SCOPE_CANARY_MAILBOX="")
    [scope] = _run("workspace.m365_scope")
    assert scope.status == "skip"
    [configured] = _run("workspace.m365_canary_configured")
    assert configured.status == "fail"
    assert "unproven" in configured.detail
    assert "ROBOTHOR_M365_SCOPE_CANARY_MAILBOX" in configured.detail


def test_a_configured_canary_passes_its_own_check(connected) -> None:
    [row] = _run("workspace.m365_canary_configured")
    assert row.status == "pass"


# ── timezone ─────────────────────────────────────────────────────────────────


def test_a_windows_zone_matching_the_instance_passes(connected) -> None:
    [row] = _run("workspace.m365_timezone")
    assert row.status == "pass", row.detail
    assert "Eastern Standard Time" in row.detail


def test_an_iana_zone_with_the_same_rules_passes(connected) -> None:
    connected.mailbox(OWNER).time_zone = "America/Detroit"
    [row] = _run("workspace.m365_timezone")
    assert row.status == "pass", row.detail


def test_a_different_zone_warns(connected) -> None:
    connected.mailbox(OWNER).time_zone = "Pacific Standard Time"
    [row] = _run("workspace.m365_timezone")
    assert row.status == "fail"
    assert "Pacific Standard Time" in row.detail
    assert "America/New_York" in row.detail


def test_an_unknown_windows_zone_is_reported_not_failed(connected) -> None:
    connected.mailbox(OWNER).time_zone = "Martian Standard Time"
    [row] = _run("workspace.m365_timezone")
    assert row.status == "pass"
    assert "could not compare" in row.detail


def test_a_timezone_the_app_may_not_read_is_reported_not_failed(connected) -> None:
    connected.fail("GET", r"/users/[^/]+/mailboxSettings/timeZone", 403)
    [row] = _run("workspace.m365_timezone")
    assert row.status == "pass"
    assert "MailboxSettings.Read" in row.detail


def test_timezone_mapping_covers_the_common_zones() -> None:
    assert workspace_m365.iana_for("Eastern Standard Time") == "America/New_York"
    assert workspace_m365.iana_for("GMT Standard Time") == "Europe/London"
    assert workspace_m365.iana_for("Europe/Berlin") == "Europe/Berlin"
    assert workspace_m365.iana_for("Not A Zone") is None
