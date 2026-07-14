from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from robothor.entity.payments import ClientPaymentMethodReference, OperationalVirtualCardReference
from robothor.entity.policy import (
    DecisionOutcome,
    DecisionReason,
    SpendPolicyEngine,
    SpendProposal,
    SpendUsage,
    TreasuryPolicy,
)

NOW = datetime(2026, 7, 13, 15, 0, tzinfo=UTC)


def _card(**updates: object) -> OperationalVirtualCardReference:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "organization_id": "org-1",
        "virtual_card_id": "card-1",
        "provider": "ramp",
        "provider_virtual_card_token": "vc_provider_A1b2c3d4",
    }
    values.update(updates)
    return OperationalVirtualCardReference.model_validate(values)


def _proposal(**updates: object) -> SpendProposal:
    values: dict[str, object] = {
        "proposal_id": "proposal-1",
        "tenant_id": "tenant-1",
        "organization_id": "org-1",
        "actor_id": "main-agent",
        "instrument": _card(),
        "purpose": "Production monitoring service",
        "vendor": "Acme Cloud",
        "category": "infrastructure",
        "currency": "USD",
        "amount": Decimal("50.00"),
        "idempotency_key": "spend-20260713-0001",
        "requested_at": NOW,
    }
    values.update(updates)
    return SpendProposal.model_validate(values)


def _policy(**updates: object) -> TreasuryPolicy:
    values: dict[str, object] = {
        "policy_id": "treasury-policy-1",
        "version": 3,
        "tenant_id": "tenant-1",
        "organization_id": "org-1",
        "enabled": True,
        "allowed_categories": {"infrastructure", "software"},
        "allowed_vendors": {"Acme Cloud"},
        "allowed_currencies": {"USD"},
        "per_transaction_limit": Decimal("100.00"),
        "daily_limit": Decimal("500.00"),
        "monthly_limit": Decimal("2000.00"),
        "approval_threshold": Decimal("75.00"),
    }
    values.update(updates)
    return TreasuryPolicy.model_validate(values)


def _usage(**updates: object) -> SpendUsage:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "organization_id": "org-1",
        "usage_date": NOW.date(),
        "daily_committed": Decimal(0),
        "monthly_committed": Decimal(0),
    }
    values.update(updates)
    return SpendUsage.model_validate(values)


def test_deny_by_default_without_an_active_policy_or_usage() -> None:
    proposal = _proposal()

    missing = SpendPolicyEngine().evaluate(proposal, policy=None, usage=_usage())
    no_usage = SpendPolicyEngine().evaluate(proposal, policy=_policy(), usage=None)

    assert (missing.outcome, missing.reason) == (
        DecisionOutcome.DENY,
        DecisionReason.POLICY_MISSING,
    )
    assert (no_usage.outcome, no_usage.reason) == (
        DecisionOutcome.DENY,
        DecisionReason.USAGE_UNAVAILABLE,
    )


def test_client_payment_reference_cannot_fund_operational_spend() -> None:
    client_reference = ClientPaymentMethodReference(
        tenant_id="tenant-1",
        organization_id="org-1",
        client_id="client-1",
        payment_method_id="method-1",
        provider="stripe",
        provider_payment_method_token="pm_external_A1b2c3d4",
    )

    with pytest.raises(ValidationError):
        _proposal(instrument=client_reference)


def test_allow_and_approval_threshold_are_deterministic() -> None:
    allow = SpendPolicyEngine().evaluate(_proposal(), policy=_policy(), usage=_usage())
    approval = SpendPolicyEngine().evaluate(
        _proposal(amount=Decimal("75.00"), idempotency_key="spend-20260713-0002"),
        policy=_policy(),
        usage=_usage(),
    )

    assert (allow.outcome, allow.reason) == (
        DecisionOutcome.ALLOW,
        DecisionReason.WITHIN_POLICY,
    )
    assert (approval.outcome, approval.reason) == (
        DecisionOutcome.APPROVAL_REQUIRED,
        DecisionReason.APPROVAL_THRESHOLD,
    )


def test_transaction_daily_and_monthly_caps_have_specific_reason_codes() -> None:
    transaction = SpendPolicyEngine().evaluate(
        _proposal(amount=Decimal("100.01")), policy=_policy(), usage=_usage()
    )
    daily = SpendPolicyEngine().evaluate(
        _proposal(idempotency_key="spend-20260713-daily"),
        policy=_policy(),
        usage=_usage(daily_committed=Decimal("460.00")),
    )
    monthly = SpendPolicyEngine().evaluate(
        _proposal(idempotency_key="spend-20260713-monthly"),
        policy=_policy(),
        usage=_usage(monthly_committed=Decimal("1980.00")),
    )

    assert transaction.reason is DecisionReason.PER_TRANSACTION_LIMIT
    assert daily.reason is DecisionReason.DAILY_LIMIT
    assert monthly.reason is DecisionReason.MONTHLY_LIMIT
    assert {transaction.outcome, daily.outcome, monthly.outcome} == {DecisionOutcome.DENY}


def test_category_vendor_currency_and_ownership_are_enforced() -> None:
    category = SpendPolicyEngine().evaluate(
        _proposal(category="travel"), policy=_policy(), usage=_usage()
    )
    vendor = SpendPolicyEngine().evaluate(
        _proposal(vendor="Other Vendor"), policy=_policy(), usage=_usage()
    )
    currency = SpendPolicyEngine().evaluate(
        _proposal(currency="EUR"), policy=_policy(), usage=_usage()
    )
    ownership = SpendPolicyEngine().evaluate(
        _proposal(instrument=_card(organization_id="other-org")),
        policy=_policy(),
        usage=_usage(),
    )

    assert category.reason is DecisionReason.CATEGORY_NOT_ALLOWED
    assert vendor.reason is DecisionReason.VENDOR_NOT_ALLOWED
    assert currency.reason is DecisionReason.CURRENCY_NOT_ALLOWED
    assert ownership.reason is DecisionReason.INSTRUMENT_OWNERSHIP_MISMATCH


def test_idempotent_retry_returns_same_decision_and_collision_is_denied() -> None:
    engine = SpendPolicyEngine()
    proposal = _proposal()

    first = engine.evaluate(proposal, policy=_policy(), usage=_usage())
    retry = engine.evaluate(proposal, policy=_policy(), usage=_usage(daily_committed=Decimal(499)))
    collision = engine.evaluate(
        _proposal(amount=Decimal("60.00")), policy=_policy(), usage=_usage()
    )

    assert retry == first
    assert retry.decision_id == first.decision_id
    assert (collision.outcome, collision.reason) == (
        DecisionOutcome.DENY,
        DecisionReason.IDEMPOTENCY_CONFLICT,
    )
