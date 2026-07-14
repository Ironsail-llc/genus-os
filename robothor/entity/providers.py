"""Provider integration contracts with no network implementation.

Adapters live outside the Entity Kernel.  They receive validated token
references only, and an operational authorization request can only be built for
an ``allow`` treasury decision.  This module intentionally cannot perform a
real charge, card issuance, or bank operation on its own.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic resolves this field at runtime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from robothor.entity.payments import (
    ClientPaymentMethodReference,
    Identifier,
    OperationalVirtualCardReference,
    ProviderName,
    validate_provider_token,
)
from robothor.entity.policy import (
    DecisionOutcome,
    PolicyDecision,
    SpendProposal,
    TreasuryPolicy,
)


class ClientPaymentMethodVerification(BaseModel):
    """Non-sensitive result of asking a provider to verify a client token."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderName
    payment_method_id: Identifier
    valid: bool
    reusable: bool = False
    reason_code: str | None = Field(default=None, max_length=100)


class VirtualCardProvisionRequest(BaseModel):
    """Controls sent to a virtual-card provider; never card credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    tenant_id: Identifier
    organization_id: Identifier
    policy: TreasuryPolicy = Field(repr=False)
    requested_by: Identifier
    label: str = Field(min_length=1, max_length=100)
    purpose: str = Field(min_length=3, max_length=500)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def expiry_must_be_timezone_aware(self) -> VirtualCardProvisionRequest:
        if self.expires_at is not None and (
            self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
        ):
            raise ValueError("expires_at must include a timezone")
        if (
            self.policy.tenant_id != self.tenant_id
            or self.policy.organization_id != self.organization_id
        ):
            raise ValueError("virtual-card policy ownership does not match the request")
        if not self.policy.enabled or not self.policy.is_complete:
            raise ValueError("virtual-card provisioning requires an active, complete policy")
        return self


class OperationalAuthorizationRequest(BaseModel):
    """Execution-bound request gated by an autonomous ``allow`` decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal: SpendProposal = Field(repr=False)
    decision: PolicyDecision

    @model_validator(mode="after")
    def require_matching_allow_decision(self) -> OperationalAuthorizationRequest:
        if self.decision.outcome is not DecisionOutcome.ALLOW:
            raise ValueError("provider authorization requires an allow policy decision")
        if (
            self.decision.proposal_id != self.proposal.proposal_id
            or self.decision.proposal_fingerprint != self.proposal.fingerprint
            or self.decision.idempotency_key != self.proposal.idempotency_key
        ):
            raise ValueError("policy decision does not match the spend proposal")
        return self


class ProviderAuthorizationReference(BaseModel):
    """Opaque provider result; safe for persistence only as a secret token."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    provider: ProviderName
    decision_id: str
    provider_authorization_token: SecretStr = Field(repr=False)

    @model_validator(mode="before")
    @classmethod
    def validate_authorization_token(cls, data: object) -> object:
        if not isinstance(data, dict) or "provider_authorization_token" not in data:
            return data
        copied = dict(data)
        copied["provider_authorization_token"] = validate_provider_token(
            copied["provider_authorization_token"]
        )
        return copied


@runtime_checkable
class ClientPaymentProviderAdapter(Protocol):
    """Boundary for validating client-owned, provider-tokenized methods."""

    provider_name: str

    async def verify_payment_method(
        self, reference: ClientPaymentMethodReference
    ) -> ClientPaymentMethodVerification: ...


@runtime_checkable
class TreasuryProviderAdapter(Protocol):
    """Boundary for virtual-card provisioning and operational authorization."""

    provider_name: str

    async def provision_virtual_card(
        self, request: VirtualCardProvisionRequest
    ) -> OperationalVirtualCardReference: ...

    async def authorize_operational_spend(
        self, request: OperationalAuthorizationRequest
    ) -> ProviderAuthorizationReference: ...

    async def suspend_virtual_card(self, reference: OperationalVirtualCardReference) -> None: ...


__all__ = [
    "ClientPaymentMethodVerification",
    "ClientPaymentProviderAdapter",
    "OperationalAuthorizationRequest",
    "ProviderAuthorizationReference",
    "TreasuryProviderAdapter",
    "VirtualCardProvisionRequest",
]
