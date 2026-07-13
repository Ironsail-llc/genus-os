from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from robothor.entity.audit import REDACTED
from robothor.entity.authority import (
    AuthorityOutcome,
    AuthorityReason,
    AuthorityTier,
    EntityAction,
    EntityActionKind,
    EntityAuthorityEngine,
    EntityAuthorityPolicy,
)
from robothor.entity.ledger import (
    InMemoryAppendOnlyTreasuryLedger,
    LedgerEventDraft,
    LedgerIdempotencyConflictError,
    TreasuryEventType,
)
from robothor.entity.payments import OperationalVirtualCardReference
from robothor.entity.policy import SpendPolicyEngine, SpendProposal, SpendUsage, TreasuryPolicy

NOW = datetime(2026, 7, 13, 15, 0, tzinfo=UTC)


def test_ledger_is_append_only_idempotent_hash_chained_and_redacted() -> None:
    token = "vc_provider_SensitiveRef987"
    draft = LedgerEventDraft(
        tenant_id="tenant-1",
        organization_id="org-1",
        event_type=TreasuryEventType.PROPOSAL_EVALUATED,
        actor_id="main-agent",
        subject_id="proposal-1",
        idempotency_key="ledger-20260713-0001",
        occurred_at=NOW,
        audit_data={
            "providerReference": token,
            "cvv": "123",
            "message": "number 4242 4242 4242 4242",
            "outcome": "deny",
        },
    )
    ledger = InMemoryAppendOnlyTreasuryLedger()

    event = ledger.append(draft)
    retry = ledger.append(draft.model_copy(update={"occurred_at": NOW.replace(hour=16)}))

    assert retry is event
    assert ledger.events() == (event,)
    assert ledger.verify_chain()
    assert token not in repr(draft)
    assert token not in repr(event)
    assert event.audit_data["providerReference"] == REDACTED
    assert event.audit_data["cvv"] == REDACTED
    assert event.audit_data["message"] == REDACTED


def test_ledger_rejects_idempotency_collision() -> None:
    ledger = InMemoryAppendOnlyTreasuryLedger()
    draft = LedgerEventDraft(
        tenant_id="tenant-1",
        organization_id="org-1",
        event_type=TreasuryEventType.PROPOSAL_EVALUATED,
        actor_id="main-agent",
        subject_id="proposal-1",
        idempotency_key="ledger-20260713-0001",
        occurred_at=NOW,
        audit_data={"outcome": "allow"},
    )
    ledger.append(draft)

    with pytest.raises(LedgerIdempotencyConflictError):
        ledger.append(draft.model_copy(update={"audit_data": {"outcome": "deny"}}))


def _authority_policy(**updates: object) -> EntityAuthorityPolicy:
    values: dict[str, object] = {
        "policy_id": "authority-policy-1",
        "tenant_id": "tenant-1",
        "organization_id": "org-1",
        "max_automatic_tier": AuthorityTier.AUTHORITY_EXPANSION,
        "allow_autonomous_controlled_spend": True,
    }
    values.update(updates)
    return EntityAuthorityPolicy.model_validate(values)


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        (
            EntityActionKind.PRODUCTION_CHANGE,
            AuthorityReason.PRODUCTION_CHANGE_REQUIRES_APPROVAL,
        ),
        (
            EntityActionKind.SELF_MODIFICATION,
            AuthorityReason.SELF_MODIFICATION_REQUIRES_APPROVAL,
        ),
        (
            EntityActionKind.AUTHORITY_CHANGE,
            AuthorityReason.AUTHORITY_CHANGE_REQUIRES_APPROVAL,
        ),
    ],
)
def test_hard_authority_boundaries_cannot_be_configured_away(
    kind: EntityActionKind, reason: AuthorityReason
) -> None:
    action = EntityAction(
        action_id=f"action-{kind.value}",
        tenant_id="tenant-1",
        organization_id="org-1",
        actor_id="main-agent",
        kind=kind,
        purpose="Improve the production service",
    )

    decision = EntityAuthorityEngine.evaluate(action, policy=_authority_policy())

    assert decision.outcome is AuthorityOutcome.APPROVAL_REQUIRED
    assert decision.reason is reason


def test_operational_spend_requires_a_matching_treasury_allow() -> None:
    card = OperationalVirtualCardReference(
        tenant_id="tenant-1",
        organization_id="org-1",
        virtual_card_id="card-1",
        provider="ramp",
        provider_virtual_card_token="vc_provider_A1b2c3d4",
    )
    proposal = SpendProposal(
        proposal_id="proposal-1",
        tenant_id="tenant-1",
        organization_id="org-1",
        actor_id="main-agent",
        instrument=card,
        purpose="Production monitoring service",
        vendor="Acme Cloud",
        category="infrastructure",
        currency="USD",
        amount=Decimal(25),
        idempotency_key="spend-20260713-0001",
        requested_at=NOW,
    )
    treasury_policy = TreasuryPolicy(
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
    usage = SpendUsage(
        tenant_id="tenant-1",
        organization_id="org-1",
        usage_date=NOW.date(),
    )
    treasury_decision = SpendPolicyEngine().evaluate(proposal, policy=treasury_policy, usage=usage)
    action = EntityAction(
        action_id="action-spend-1",
        tenant_id="tenant-1",
        organization_id="org-1",
        actor_id="main-agent",
        kind=EntityActionKind.OPERATIONAL_SPEND,
        purpose="Pay for production monitoring",
        subject_id=proposal.proposal_id,
    )

    missing = EntityAuthorityEngine.evaluate(action, policy=_authority_policy())
    mismatch = EntityAuthorityEngine.evaluate(
        action.model_copy(update={"subject_id": "different-proposal"}),
        policy=_authority_policy(),
        treasury_decision=treasury_decision,
    )
    allowed = EntityAuthorityEngine.evaluate(
        action, policy=_authority_policy(), treasury_decision=treasury_decision
    )

    assert (missing.outcome, missing.reason) == (
        AuthorityOutcome.DENY,
        AuthorityReason.TREASURY_DECISION_REQUIRED,
    )
    assert (allowed.outcome, allowed.reason) == (
        AuthorityOutcome.ALLOW,
        AuthorityReason.WITHIN_DELEGATED_AUTHORITY,
    )
    assert (mismatch.outcome, mismatch.reason) == (
        AuthorityOutcome.DENY,
        AuthorityReason.TREASURY_DECISION_MISMATCH,
    )
