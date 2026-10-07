"""What ``genus workspace connect microsoft365`` hands an Entra/Exchange admin.

Three things, all pure (no vault, no network, no settings) so the CLI and its
tests share them:

* :func:`generate_certificate` -- the app's credential: an RSA key and a
  self-signed X.509 certificate. The certificate (public) is uploaded to the
  Entra app registration; the private key goes to the vault and nowhere else.
* :data:`REQUIRED_PERMISSIONS` -- the Microsoft Graph *application*
  permissions the mail and calendar transport uses.
* :func:`rbac_commands` / :func:`legacy_policy_commands` -- the Exchange
  Online PowerShell that limits the app to exactly the assistant's and the
  owner's mailboxes. Without it an app-only grant covers every mailbox in the
  company; the doctor's canary check (``workspace.m365_scope``) is what proves
  it was run.

Every mailbox address is validated by :func:`validate_mailbox` before it is
interpolated into PowerShell: an address is spliced into a single-quoted OPATH
filter, and a quote in it would widen the scope rather than narrow it.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import re
from dataclasses import dataclass, field

from robothor.workspace.errors import AuthError

__all__ = [
    "CERTIFICATE_DAYS",
    "OPTIONAL_PERMISSIONS",
    "REQUIRED_PERMISSIONS",
    "SCOPE_NAME",
    "AppCertificate",
    "generate_certificate",
    "legacy_policy_commands",
    "load_certificate",
    "rbac_commands",
    "validate_mailbox",
]

#: Graph application permissions the mail and calendar tools need.
REQUIRED_PERMISSIONS = ("Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite")

#: Read by the doctor's timezone comparison only; everything works without it.
OPTIONAL_PERMISSIONS = ("MailboxSettings.Read",)

#: The Exchange management scope (and mail-enabled group, on the legacy path).
SCOPE_NAME = "Genus OS assistant mailboxes"

#: Self-signed certificate lifetime. Re-run the command with ``--rotate``
#: before it lapses; the doctor's token step fails once Entra refuses it.
CERTIFICATE_DAYS = 365

#: RSA modulus size. 3072 keeps the key good past 2030 (NIST SP 800-57).
KEY_SIZE = 3072

_MAILBOX = re.compile(
    r"^[a-z0-9._%+-]{1,64}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def validate_mailbox(value: str) -> str:
    """A lower-cased SMTP address, or :class:`ValueError`.

    Deliberately narrower than RFC 5322: no quotes, spaces or control
    characters, because the value is spliced into an Exchange OPATH filter
    and a Graph URL path.
    """
    raw = str(value or "")
    if any(ch < " " or ch == "\x7f" for ch in raw):
        raise ValueError("a mailbox address may not contain a control character")
    candidate = raw.strip().lower()
    if not _MAILBOX.fullmatch(candidate):
        raise ValueError(f"not a plain mailbox address: {value!r}")
    return candidate


def _validate_client_id(client_id: str) -> str:
    candidate = str(client_id or "").strip().lower()
    if not _GUID.fullmatch(candidate):
        raise ValueError("the client id must be the application (client) GUID")
    return candidate


@dataclass(frozen=True)
class AppCertificate:
    """The app credential. ``repr`` never shows the private key."""

    certificate_pem: str
    private_key_pem: str = field(repr=False)
    #: Hex, upper case: what the Entra portal lists under "Thumbprint".
    thumbprint_sha1: str = ""
    thumbprint_sha256: str = ""
    #: Base64 DER: what a manifest's ``keyCredentials[].key`` carries.
    certificate_base64: str = ""
    not_after: datetime.datetime | None = None
    subject: str = ""


def _describe(cert: object, private_key_pem: str) -> AppCertificate:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    assert isinstance(cert, x509.Certificate)
    der = cert.public_bytes(serialization.Encoding.DER)
    return AppCertificate(
        certificate_pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
        private_key_pem=private_key_pem,
        thumbprint_sha1=cert.fingerprint(hashes.SHA1()).hex().upper(),  # noqa: S303 - Entra's identifier
        thumbprint_sha256=hashlib.sha256(der).hexdigest().upper(),
        certificate_base64=base64.b64encode(der).decode(),
        not_after=cert.not_valid_after_utc,
        subject=cert.subject.rfc4514_string(),
    )


def generate_certificate(
    common_name: str = "Genus OS workspace", *, days: int = CERTIFICATE_DAYS
) -> AppCertificate:
    """A new RSA key and a self-signed certificate for it."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return _describe(cert, key_pem)


def load_certificate(certificate_pem: str, private_key_pem: str) -> AppCertificate:
    """A stored pair, checked to belong together. Raises :class:`AuthError`."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    try:
        cert = x509.load_pem_x509_certificate(str(certificate_pem or "").encode())
        key = serialization.load_pem_private_key(str(private_key_pem or "").encode(), password=None)
    except Exception:  # noqa: BLE001 - never echo PEM material
        raise AuthError("the stored microsoft365 certificate or key could not be read") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise AuthError("the stored microsoft365 private key must be RSA")
    if cert.public_key().public_numbers() != key.public_key().public_numbers():  # type: ignore[union-attr]
        raise AuthError("the stored microsoft365 private key does not match the certificate")
    return _describe(cert, private_key_pem)


def _mailboxes(*addresses: str) -> list[str]:
    return [validate_mailbox(address) for address in addresses]


def rbac_commands(client_id: str, assistant: str, owner: str, canary: str = "") -> str:
    """Exchange Online PowerShell: RBAC for Applications, scoped to two mailboxes."""
    app = _validate_client_id(client_id)
    assistant, owner = _mailboxes(assistant, owner)
    canary_line = (
        f"Test-ServicePrincipalAuthorization -Identity {app} -Resource {validate_mailbox(canary)}"
        "   # every role must show InScope False"
        if canary
        else "# Set a canary mailbox and run Test-ServicePrincipalAuthorization against it."
    )
    roles = [f"Application {permission}" for permission in REQUIRED_PERMISSIONS] + [
        f"Application {permission}" for permission in OPTIONAL_PERMISSIONS
    ]
    assignments = "\n".join(
        f'New-ManagementRoleAssignment -App {app} -Role "{role}" '
        f'-CustomResourceScope "{SCOPE_NAME}"'
        + ("   # optional: timezone check" if role.endswith(OPTIONAL_PERMISSIONS) else "")
        for role in roles
    )
    return "\n".join(
        [
            "Connect-ExchangeOnline -UserPrincipalName <exchange-admin@your-domain>",
            "",
            "# The ObjectId is the ENTERPRISE APPLICATION (service principal) object id,",
            "# not the app registration's object id.",
            f"New-ServicePrincipal -AppId {app} -ObjectId <enterprise-app-object-id> "
            '-DisplayName "Genus OS assistant"',
            "",
            f'New-ManagementScope -Name "{SCOPE_NAME}" '
            f"-RecipientRestrictionFilter \"PrimarySmtpAddress -eq '{assistant}' "
            f"-or PrimarySmtpAddress -eq '{owner}'\"",
            "",
            assignments,
            "",
            f"Test-ServicePrincipalAuthorization -Identity {app} -Resource {assistant}"
            "   # InScope True",
            canary_line,
        ]
    )


def legacy_policy_commands(client_id: str, assistant: str, owner: str, canary: str = "") -> str:
    """The older alternative: an ApplicationAccessPolicy over a mail-enabled security group."""
    app = _validate_client_id(client_id)
    assistant, owner = _mailboxes(assistant, owner)
    alias = "genus-os-assistant-mailboxes"
    lines = [
        "Connect-ExchangeOnline -UserPrincipalName <exchange-admin@your-domain>",
        "",
        f'New-DistributionGroup -Name "{SCOPE_NAME}" -Alias {alias} -Type Security '
        f"-Members {assistant},{owner}",
        "",
        f"New-ApplicationAccessPolicy -AppId {app} -PolicyScopeGroupId {alias} "
        '-AccessRight RestrictAccess -Description "Genus OS: assistant and owner only"',
        "",
        f"Test-ApplicationAccessPolicy -Identity {assistant} -AppId {app}   # Granted",
    ]
    if canary:
        lines.append(
            f"Test-ApplicationAccessPolicy -Identity {validate_mailbox(canary)} -AppId {app}"
            "   # Denied"
        )
    return "\n".join(lines)
