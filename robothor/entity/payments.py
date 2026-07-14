"""Token-reference-only payment models.

Genus OS keeps customer payment methods and its own operational virtual cards
as separate domain types.  Neither type accepts or stores PAN, expiry, magnetic
stripe data, cryptograms, or card verification values.  Opaque references are
credentials and therefore use :class:`pydantic.SecretStr` and are excluded from
model representations.
"""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
)

from robothor.entity.audit import AuditValue  # noqa: TC001 - audit schema is public at runtime

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
ProviderName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        min_length=2,
        max_length=32,
        pattern=r"^[a-z][a-z0-9_-]*$",
    ),
]

_PROVIDER_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,254}$")
_CARD_DIGITS = re.compile(r"^[\d -]+$")
_EMBEDDED_CARD_DIGITS = re.compile(r"(?<!\d)(?:\d[ -]?){12,19}(?!\d)")


def validate_provider_token(value: object) -> SecretStr:
    """Validate an opaque provider reference without returning it as plain text.

    Provider references must be printable, whitespace-free, and at least eight
    characters.  Any value consisting of 12--19 digits after common card
    separators is rejected whether or not it passes Luhn validation.
    """

    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    if not isinstance(raw, str):
        raise ValueError("provider reference must be a string")
    if not _PROVIDER_TOKEN.fullmatch(raw):
        raise ValueError("provider reference must be an opaque 8-255 character token")
    compact = re.sub(r"[ -]", "", raw)
    contains_pan = any(
        12 <= len(re.sub(r"\D", "", candidate)) <= 19
        for candidate in _EMBEDDED_CARD_DIGITS.findall(raw)
    )
    if contains_pan or (
        _CARD_DIGITS.fullmatch(raw) and compact.isdigit() and 12 <= len(compact) <= 19
    ):
        raise ValueError("raw payment card numbers are prohibited; use a provider token")
    return SecretStr(raw)


def token_fingerprint(value: SecretStr) -> str:
    """Return a non-reversible correlation fingerprint for a high-entropy token."""

    return hashlib.sha256(value.get_secret_value().encode("utf-8")).hexdigest()[:16]


class _PaymentReference(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        hide_input_in_errors=True,
    )

    tenant_id: Identifier
    organization_id: Identifier
    provider: ProviderName


class ClientPaymentMethodReference(_PaymentReference):
    """A customer's provider-tokenized payment method.

    This type is never a source of funds for Genus operational spend.
    """

    reference_type: Literal["client_payment_method"] = "client_payment_method"
    client_id: Identifier
    payment_method_id: Identifier
    provider_payment_method_token: SecretStr = Field(repr=False)

    _validate_token = field_validator("provider_payment_method_token", mode="before")(
        validate_provider_token
    )

    @property
    def token_fingerprint(self) -> str:
        return token_fingerprint(self.provider_payment_method_token)

    def to_audit_dict(self) -> dict[str, AuditValue]:
        return {
            "reference_type": self.reference_type,
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "client_id": self.client_id,
            "payment_method_id": self.payment_method_id,
            "provider": self.provider,
            "token_fingerprint": self.token_fingerprint,
        }


class OperationalVirtualCardReference(_PaymentReference):
    """A provider-issued virtual card owned by the Genus organization.

    ``virtual_card_id`` is Genus' internal identifier.  The provider token is
    disclosed only to a provider adapter at the execution boundary.
    """

    reference_type: Literal["operational_virtual_card"] = "operational_virtual_card"
    virtual_card_id: Identifier
    provider_virtual_card_token: SecretStr = Field(repr=False)
    last_four: str | None = Field(default=None, pattern=r"^\d{4}$", repr=False)
    brand: str | None = Field(default=None, min_length=1, max_length=32)
    active: bool = True

    _validate_token = field_validator("provider_virtual_card_token", mode="before")(
        validate_provider_token
    )

    @property
    def token_fingerprint(self) -> str:
        return token_fingerprint(self.provider_virtual_card_token)

    def to_audit_dict(self) -> dict[str, AuditValue]:
        result: dict[str, AuditValue] = {
            "reference_type": self.reference_type,
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "virtual_card_id": self.virtual_card_id,
            "provider": self.provider,
            "token_fingerprint": self.token_fingerprint,
            "active": self.active,
        }
        if self.last_four is not None:
            result["card_display"] = f"****{self.last_four}"
        if self.brand is not None:
            result["brand"] = self.brand
        return result


__all__ = [
    "ClientPaymentMethodReference",
    "Identifier",
    "OperationalVirtualCardReference",
    "ProviderName",
    "token_fingerprint",
    "validate_provider_token",
]
