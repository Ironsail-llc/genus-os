from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from robothor.entity.payments import OperationalVirtualCardReference
from robothor.entity.policy import SpendPolicyEngine, SpendProposal, SpendUsage, TreasuryPolicy
from robothor.entity.providers import (
    OperationalAuthorizationRequest,
    ProviderAuthorizationReference,
    VirtualCardProvisionRequest,
)

NOW = datetime(2026, 7, 13, 15, 0, tzinfo=UTC)


def _policy() -> TreasuryPolicy:
    return TreasuryPolicy(
        policy_id="treasury-policy-1",
        tenant_id="tenant-1",
        organization_id="org-1",
        enabled=True,
        allowed_categories={"infrastructure"},
        allowed_vendors={"Acme Cloud"},
        allowed_currencies={"USD"},
        per_transaction_limit=Decimal(100),
        daily_limit=Decimal(500),
        monthly_limit=Decimal(2000),
        approval_threshold=Decimal(75),
    )


def _proposal(amount: Decimal = Decimal(25)) -> SpendProposal:
    return SpendProposal(
        proposal_id="proposal-1",
        tenant_id="tenant-1",
        organization_id="org-1",
        actor_id="main-agent",
        instrument=OperationalVirtualCardReference(
            tenant_id="tenant-1",
            organization_id="org-1",
            virtual_card_id="card-1",
            provider="ramp",
            provider_virtual_card_token="vc_provider_A1b2c3d4",
        ),
        purpose="Production monitoring service",
        vendor="Acme Cloud",
        category="infrastructure",
        currency="USD",
        amount=amount,
        idempotency_key="spend-20260713-0001",
        requested_at=NOW,
    )


def test_virtual_card_provisioning_is_bound_to_active_owned_policy() -> None:
    request = VirtualCardProvisionRequest(
        tenant_id="tenant-1",
        organization_id="org-1",
        policy=_policy(),
        requested_by="main-agent",
        label="Infrastructure card",
        purpose="Pay allowlisted infrastructure vendors",
    )

    assert request.policy.policy_id == "treasury-policy-1"
    with pytest.raises(ValidationError, match="ownership"):
        VirtualCardProvisionRequest(
            tenant_id="tenant-1",
            organization_id="other-org",
            policy=_policy(),
            requested_by="main-agent",
            label="Infrastructure card",
            purpose="Pay allowlisted infrastructure vendors",
        )


def test_provider_authorization_requires_matching_allow_decision() -> None:
    allowed_proposal = _proposal()
    allowed = SpendPolicyEngine().evaluate(
        allowed_proposal,
        policy=_policy(),
        usage=SpendUsage(
            tenant_id="tenant-1",
            organization_id="org-1",
            usage_date=NOW.date(),
        ),
    )
    approval_proposal = _proposal(amount=Decimal(75)).model_copy(
        update={"idempotency_key": "spend-20260713-0002"}
    )
    approval = SpendPolicyEngine().evaluate(
        approval_proposal,
        policy=_policy(),
        usage=SpendUsage(
            tenant_id="tenant-1",
            organization_id="org-1",
            usage_date=NOW.date(),
        ),
    )

    OperationalAuthorizationRequest(proposal=allowed_proposal, decision=allowed)
    with pytest.raises(ValidationError, match="requires an allow"):
        OperationalAuthorizationRequest(proposal=approval_proposal, decision=approval)


def test_provider_authorization_reference_is_token_validated_and_redacted() -> None:
    token = "auth_provider_SensitiveRef987"
    reference = ProviderAuthorizationReference(
        provider="ramp",
        decision_id="decision-1",
        provider_authorization_token=token,
    )

    assert token not in repr(reference)
    assert token not in reference.model_dump_json()
    with pytest.raises(ValidationError) as exc_info:
        ProviderAuthorizationReference(
            provider="ramp",
            decision_id="decision-1",
            provider_authorization_token="4242424242424242",
        )
    assert "4242424242424242" not in str(exc_info.value)
