"""The connect helpers: the app certificate and the scope commands an admin runs."""

from __future__ import annotations

import base64
import hashlib

import pytest

from robothor.workspace.errors import AuthError
from robothor.workspace.microsoft import connect
from robothor.workspace.microsoft.auth import ClientCredentialTokenSource
from robothor.workspace.tests.fake_graph import CLIENT_ID, TENANT_ID, FakeGraphTenant

ASSISTANT = "assistant@example.com"
OWNER = "owner@example.com"
CANARY = "canary@example.com"


def test_generated_certificate_is_rsa_self_signed_and_signs_a_real_assertion() -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    cert = connect.generate_certificate()
    parsed = x509.load_pem_x509_certificate(cert.certificate_pem.encode())
    assert parsed.public_key().key_size >= 2048  # type: ignore[union-attr]
    assert parsed.issuer == parsed.subject
    der = parsed.public_bytes(serialization.Encoding.DER)
    assert cert.thumbprint_sha1 == parsed.fingerprint(hashes.SHA1()).hex().upper()  # noqa: S303
    assert cert.thumbprint_sha256 == hashlib.sha256(der).hexdigest().upper()
    assert base64.b64decode(cert.certificate_base64) == der
    assert "PRIVATE KEY" in cert.private_key_pem
    assert "PRIVATE KEY" not in repr(cert)


async def test_the_generated_pair_is_accepted_by_entra() -> None:
    cert = connect.generate_certificate()
    tenant = FakeGraphTenant(certificate_pem=cert.certificate_pem)
    source = ClientCredentialTokenSource(
        TENANT_ID,
        CLIENT_ID,
        certificate_pem=cert.certificate_pem,
        private_key_pem=cert.private_key_pem,
        transport=tenant.transport(),
    )
    assert (await source.token()).startswith("fake-graph-token-")


def test_load_certificate_round_trips_and_rejects_a_mismatched_key() -> None:
    first = connect.generate_certificate()
    second = connect.generate_certificate()
    loaded = connect.load_certificate(first.certificate_pem, first.private_key_pem)
    assert loaded.thumbprint_sha1 == first.thumbprint_sha1
    with pytest.raises(AuthError, match="does not match"):
        connect.load_certificate(first.certificate_pem, second.private_key_pem)
    with pytest.raises(AuthError):
        connect.load_certificate("not a pem", first.private_key_pem)


@pytest.mark.parametrize(
    "value",
    [
        "assistant@example.com",
        "First.Last+genus@sub.contoso.example",
    ],
)
def test_mailbox_validation_accepts_addresses(value: str) -> None:
    assert connect.validate_mailbox(value) == value.lower()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "assistant",
        "o'brien@example.com",
        "a@example.com' -or PrimarySmtpAddress -like '*",
        'a"b@example.com',
        "a@example.com\n",
        "a b@example.com",
    ],
)
def test_mailbox_validation_refuses_anything_that_could_break_out_of_a_filter(value: str) -> None:
    with pytest.raises(ValueError):
        connect.validate_mailbox(value)


def test_rbac_commands_scope_exactly_the_two_mailboxes() -> None:
    script = connect.rbac_commands(CLIENT_ID, ASSISTANT, OWNER, CANARY)
    assert "Connect-ExchangeOnline" in script
    assert f"New-ServicePrincipal -AppId {CLIENT_ID}" in script
    assert "New-ManagementScope" in script
    assert (
        f"-RecipientRestrictionFilter \"PrimarySmtpAddress -eq '{ASSISTANT}' "
        f"-or PrimarySmtpAddress -eq '{OWNER}'\""
    ) in script
    for role in (
        "Application Mail.ReadWrite",
        "Application Mail.Send",
        "Application Calendars.ReadWrite",
    ):
        assert f'-Role "{role}"' in script
    assert script.count("-CustomResourceScope") == 4  # three required + mailbox settings
    assert f"Test-ServicePrincipalAuthorization -Identity {CLIENT_ID} -Resource {CANARY}" in script


def test_legacy_policy_commands_restrict_to_a_group() -> None:
    script = connect.legacy_policy_commands(CLIENT_ID, ASSISTANT, OWNER, CANARY)
    assert "New-DistributionGroup" in script and "-Type Security" in script
    assert f"New-ApplicationAccessPolicy -AppId {CLIENT_ID}" in script
    assert "-AccessRight RestrictAccess" in script
    assert f"Test-ApplicationAccessPolicy -Identity {CANARY} -AppId {CLIENT_ID}" in script


def test_permissions_are_the_three_graph_application_permissions() -> None:
    assert connect.REQUIRED_PERMISSIONS == ("Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite")
    assert connect.OPTIONAL_PERMISSIONS == ("MailboxSettings.Read",)
