"""Shared exact-amount decisions; ownership and reservations stay with each domain."""

from decimal import Decimal
from enum import StrEnum

Amount = int | Decimal


class DecisionOutcome(StrEnum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


class DecisionReason(StrEnum):
    WITHIN_POLICY = "within_policy"
    APPROVAL_THRESHOLD = "approval_threshold"
    POLICY_MISSING = "policy_missing"
    POLICY_DISABLED = "policy_disabled"
    POLICY_INCOMPLETE = "policy_incomplete"
    POLICY_TENANT_MISMATCH = "policy_tenant_mismatch"
    POLICY_ORGANIZATION_MISMATCH = "policy_organization_mismatch"
    INSTRUMENT_OWNERSHIP_MISMATCH = "instrument_ownership_mismatch"
    INSTRUMENT_INACTIVE = "instrument_inactive"
    CATEGORY_NOT_ALLOWED = "category_not_allowed"
    VENDOR_NOT_ALLOWED = "vendor_not_allowed"
    CURRENCY_NOT_ALLOWED = "currency_not_allowed"
    PER_TRANSACTION_LIMIT = "per_transaction_limit"
    USAGE_UNAVAILABLE = "usage_unavailable"
    USAGE_SNAPSHOT_MISMATCH = "usage_snapshot_mismatch"
    DAILY_LIMIT = "daily_limit"
    MONTHLY_LIMIT = "monthly_limit"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


def _valid(value: object) -> bool:
    return (
        isinstance(value, (int, Decimal))
        and not isinstance(value, bool)
        and (not isinstance(value, Decimal) or value.is_finite())
        and value >= 0
    )


def within_limit(used: Amount, amount: Amount, limit: Amount) -> bool:
    """Unknown, floating-point, negative or nonfinite inputs cannot grant authority."""
    return all(_valid(v) for v in (used, amount, limit)) and used + amount <= limit


def decide_limits(
    *,
    amount: Amount,
    per_transaction_limit: Amount,
    monthly_limit: Amount,
    monthly_used: Amount | None,
    daily_limit: Amount | None = None,
    daily_used: Amount | None = None,
    usage_matches: bool = True,
    approval_threshold: Amount | None = None,
) -> tuple[DecisionOutcome, DecisionReason]:
    if not within_limit(0, amount, per_transaction_limit):
        return DecisionOutcome.DENY, DecisionReason.PER_TRANSACTION_LIMIT
    if not _valid(monthly_used) or (daily_limit is not None and not _valid(daily_used)):
        return DecisionOutcome.DENY, DecisionReason.USAGE_UNAVAILABLE
    if not usage_matches:
        return DecisionOutcome.DENY, DecisionReason.USAGE_SNAPSHOT_MISMATCH
    if daily_limit is not None and (
        daily_used is None or not within_limit(daily_used, amount, daily_limit)
    ):
        return DecisionOutcome.DENY, DecisionReason.DAILY_LIMIT
    if monthly_used is None or not within_limit(monthly_used, amount, monthly_limit):
        return DecisionOutcome.DENY, DecisionReason.MONTHLY_LIMIT
    if approval_threshold is not None:
        if not _valid(approval_threshold):
            return DecisionOutcome.DENY, DecisionReason.POLICY_INCOMPLETE
        if amount >= approval_threshold:
            return DecisionOutcome.APPROVAL_REQUIRED, DecisionReason.APPROVAL_THRESHOLD
    return DecisionOutcome.ALLOW, DecisionReason.WITHIN_POLICY
