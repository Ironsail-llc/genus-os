"""Deterministic, deny-by-default treasury policy decisions."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from robothor.entity.audit import AuditValue  # noqa: TC001 - audit schema is public at runtime
from robothor.entity.payments import (  # noqa: TC001 - Pydantic runtime fields
    Identifier,
    OperationalVirtualCardReference,
)

Category = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    ),
]
Currency = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_upper=True,
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
    ),
]
IdempotencyKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]

_MAX_AMOUNT = Decimal("99999999999999.9999")


def _money(value: object, *, allow_zero: bool) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError("money values must use Decimal, integer, or a decimal string")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid money value") from exc
    if not amount.is_finite():
        raise ValueError("money values must be finite")
    if amount < 0 or (not allow_zero and amount == 0):
        comparison = "non-negative" if allow_zero else "greater than zero"
        raise ValueError(f"money values must be {comparison}")
    if amount > _MAX_AMOUNT:
        raise ValueError("money value exceeds the supported maximum")
    exponent = amount.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError("money values must be finite")
    if exponent < -4:
        raise ValueError("money values support at most four decimal places")
    return amount


def _positive_money(value: object) -> Decimal:
    return _money(value, allow_zero=False)


def _non_negative_money(value: object) -> Decimal:
    return _money(value, allow_zero=True)


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


class SpendProposal(BaseModel):
    """A proposed operational purchase, not a provider authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    proposal_id: Identifier
    tenant_id: Identifier
    organization_id: Identifier
    actor_id: Identifier
    instrument: OperationalVirtualCardReference
    purpose: str = Field(min_length=3, max_length=500)
    vendor: str = Field(min_length=1, max_length=200)
    category: Category
    currency: Currency
    amount: Decimal
    idempotency_key: IdempotencyKey
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    _validate_amount = field_validator("amount", mode="before")(_positive_money)

    @field_validator("requested_at")
    @classmethod
    def requested_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("requested_at must include a timezone")
        return value

    @property
    def fingerprint(self) -> str:
        """Stable semantic fingerprint used for idempotency collision checks."""

        payload = {
            "proposal_id": self.proposal_id,
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "actor_id": self.actor_id,
            "instrument_id": self.instrument.virtual_card_id,
            "instrument_token_fingerprint": self.instrument.token_fingerprint,
            "purpose": self.purpose,
            "vendor": self.vendor,
            "category": self.category,
            "currency": self.currency,
            "amount": format(self.amount, "f"),
            "idempotency_key": self.idempotency_key,
            "requested_at": self.requested_at.isoformat(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def to_audit_dict(self) -> dict[str, AuditValue]:
        return {
            "proposal_id": self.proposal_id,
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "actor_id": self.actor_id,
            "instrument_id": self.instrument.virtual_card_id,
            "purpose": self.purpose,
            "vendor": self.vendor,
            "category": self.category,
            "currency": self.currency,
            "amount": format(self.amount, "f"),
            "idempotency_key": self.idempotency_key,
            "requested_at": self.requested_at.isoformat(),
        }


class TreasuryPolicy(BaseModel):
    """Organization-owned spend authority.

    Policies default to disabled and have empty allowlists.  Merely constructing
    a policy therefore grants no spending authority.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    policy_id: Identifier
    version: int = Field(default=1, ge=1)
    tenant_id: Identifier
    organization_id: Identifier
    enabled: bool = False
    allowed_categories: frozenset[Category] = frozenset()
    allowed_vendors: frozenset[str] = frozenset()
    allowed_currencies: frozenset[Currency] = frozenset()
    per_transaction_limit: Decimal = Decimal(0)
    daily_limit: Decimal = Decimal(0)
    monthly_limit: Decimal = Decimal(0)
    approval_threshold: Decimal = Decimal(0)

    _validate_limits = field_validator(
        "per_transaction_limit", "daily_limit", "monthly_limit", mode="before"
    )(_non_negative_money)
    _validate_threshold = field_validator("approval_threshold", mode="before")(_non_negative_money)

    @field_validator("allowed_vendors")
    @classmethod
    def normalise_vendors(cls, values: frozenset[str]) -> frozenset[str]:
        normalised = frozenset(value.strip().casefold() for value in values if value.strip())
        if any(len(value) > 200 for value in normalised):
            raise ValueError("allowed vendor names must be 200 characters or fewer")
        return normalised

    @model_validator(mode="after")
    def validate_limit_order(self) -> TreasuryPolicy:
        if self.daily_limit and self.per_transaction_limit > self.daily_limit:
            raise ValueError("per_transaction_limit cannot exceed daily_limit")
        if self.monthly_limit and self.daily_limit > self.monthly_limit:
            raise ValueError("daily_limit cannot exceed monthly_limit")
        if self.per_transaction_limit and self.approval_threshold > self.per_transaction_limit:
            raise ValueError("approval_threshold cannot exceed per_transaction_limit")
        return self

    @property
    def is_complete(self) -> bool:
        return bool(
            self.allowed_categories
            and self.allowed_vendors
            and self.allowed_currencies
            and self.per_transaction_limit > 0
            and self.daily_limit > 0
            and self.monthly_limit > 0
        )


class SpendUsage(BaseModel):
    """Committed-and-reserved spend for the proposal's accounting windows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: Identifier
    organization_id: Identifier
    usage_date: date
    daily_committed: Decimal = Decimal(0)
    monthly_committed: Decimal = Decimal(0)

    _validate_usage = field_validator("daily_committed", "monthly_committed", mode="before")(
        _non_negative_money
    )


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    proposal_id: Identifier
    proposal_fingerprint: str
    idempotency_key: IdempotencyKey
    policy_id: Identifier | None = None
    policy_version: int | None = None
    outcome: DecisionOutcome
    reason: DecisionReason

    @property
    def requires_approval(self) -> bool:
        return self.outcome is DecisionOutcome.APPROVAL_REQUIRED

    def to_audit_dict(self) -> dict[str, AuditValue]:
        return {
            "decision_id": self.decision_id,
            "proposal_id": self.proposal_id,
            "proposal_fingerprint": self.proposal_fingerprint,
            "idempotency_key": self.idempotency_key,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "outcome": self.outcome.value,
            "reason": self.reason.value,
        }


@dataclass(frozen=True, slots=True)
class StoredDecision:
    proposal_fingerprint: str
    decision: PolicyDecision


@runtime_checkable
class DecisionStore(Protocol):
    """Persistence boundary required for restart-safe idempotency."""

    def get(self, scope: str, idempotency_key: str) -> StoredDecision | None: ...

    def put_if_absent(
        self, scope: str, idempotency_key: str, record: StoredDecision
    ) -> StoredDecision: ...


class InMemoryDecisionStore:
    """Thread-safe test/local store; production deployments should persist this."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], StoredDecision] = {}
        self._lock = threading.Lock()

    def get(self, scope: str, idempotency_key: str) -> StoredDecision | None:
        with self._lock:
            return self._records.get((scope, idempotency_key))

    def put_if_absent(
        self, scope: str, idempotency_key: str, record: StoredDecision
    ) -> StoredDecision:
        with self._lock:
            return self._records.setdefault((scope, idempotency_key), record)


class SpendPolicyEngine:
    """Evaluate proposals using a fixed-order, fail-closed decision table."""

    def __init__(self, store: DecisionStore | None = None) -> None:
        self._store = store or InMemoryDecisionStore()

    def evaluate(
        self,
        proposal: SpendProposal,
        *,
        policy: TreasuryPolicy | None,
        usage: SpendUsage | None,
    ) -> PolicyDecision:
        scope = f"{proposal.tenant_id}:{proposal.organization_id}"
        prior = self._store.get(scope, proposal.idempotency_key)
        if prior is not None:
            if prior.proposal_fingerprint == proposal.fingerprint:
                return prior.decision
            return self._decision(
                proposal, policy, DecisionOutcome.DENY, DecisionReason.IDEMPOTENCY_CONFLICT
            )

        outcome, reason = self._evaluate_uncached(proposal, policy, usage)
        candidate = self._decision(proposal, policy, outcome, reason)
        record = StoredDecision(proposal_fingerprint=proposal.fingerprint, decision=candidate)
        winner = self._store.put_if_absent(scope, proposal.idempotency_key, record)
        if winner.proposal_fingerprint == proposal.fingerprint:
            return winner.decision
        return self._decision(
            proposal,
            policy,
            DecisionOutcome.DENY,
            DecisionReason.IDEMPOTENCY_CONFLICT,
        )

    @staticmethod
    def _evaluate_uncached(
        proposal: SpendProposal,
        policy: TreasuryPolicy | None,
        usage: SpendUsage | None,
    ) -> tuple[DecisionOutcome, DecisionReason]:
        if policy is None:
            return DecisionOutcome.DENY, DecisionReason.POLICY_MISSING
        if not policy.enabled:
            return DecisionOutcome.DENY, DecisionReason.POLICY_DISABLED
        if not policy.is_complete:
            return DecisionOutcome.DENY, DecisionReason.POLICY_INCOMPLETE
        if proposal.tenant_id != policy.tenant_id:
            return DecisionOutcome.DENY, DecisionReason.POLICY_TENANT_MISMATCH
        if proposal.organization_id != policy.organization_id:
            return DecisionOutcome.DENY, DecisionReason.POLICY_ORGANIZATION_MISMATCH
        if (
            proposal.instrument.tenant_id != proposal.tenant_id
            or proposal.instrument.organization_id != proposal.organization_id
        ):
            return DecisionOutcome.DENY, DecisionReason.INSTRUMENT_OWNERSHIP_MISMATCH
        if not proposal.instrument.active:
            return DecisionOutcome.DENY, DecisionReason.INSTRUMENT_INACTIVE
        if proposal.category not in policy.allowed_categories:
            return DecisionOutcome.DENY, DecisionReason.CATEGORY_NOT_ALLOWED
        if proposal.vendor.casefold() not in policy.allowed_vendors:
            return DecisionOutcome.DENY, DecisionReason.VENDOR_NOT_ALLOWED
        if proposal.currency not in policy.allowed_currencies:
            return DecisionOutcome.DENY, DecisionReason.CURRENCY_NOT_ALLOWED
        if proposal.amount > policy.per_transaction_limit:
            return DecisionOutcome.DENY, DecisionReason.PER_TRANSACTION_LIMIT
        if usage is None:
            return DecisionOutcome.DENY, DecisionReason.USAGE_UNAVAILABLE
        if (
            usage.tenant_id != proposal.tenant_id
            or usage.organization_id != proposal.organization_id
            or usage.usage_date != proposal.requested_at.date()
        ):
            return DecisionOutcome.DENY, DecisionReason.USAGE_SNAPSHOT_MISMATCH
        if usage.daily_committed + proposal.amount > policy.daily_limit:
            return DecisionOutcome.DENY, DecisionReason.DAILY_LIMIT
        if usage.monthly_committed + proposal.amount > policy.monthly_limit:
            return DecisionOutcome.DENY, DecisionReason.MONTHLY_LIMIT
        if proposal.amount >= policy.approval_threshold:
            return DecisionOutcome.APPROVAL_REQUIRED, DecisionReason.APPROVAL_THRESHOLD
        return DecisionOutcome.ALLOW, DecisionReason.WITHIN_POLICY

    @staticmethod
    def _decision(
        proposal: SpendProposal,
        policy: TreasuryPolicy | None,
        outcome: DecisionOutcome,
        reason: DecisionReason,
    ) -> PolicyDecision:
        policy_identity = "none" if policy is None else f"{policy.policy_id}:{policy.version}"
        deterministic_name = ":".join(
            (proposal.fingerprint, policy_identity, outcome.value, reason.value)
        )
        decision_id = str(uuid.uuid5(uuid.NAMESPACE_URL, deterministic_name))
        return PolicyDecision(
            decision_id=decision_id,
            proposal_id=proposal.proposal_id,
            proposal_fingerprint=proposal.fingerprint,
            idempotency_key=proposal.idempotency_key,
            policy_id=policy.policy_id if policy else None,
            policy_version=policy.version if policy else None,
            outcome=outcome,
            reason=reason,
        )


__all__ = [
    "DecisionOutcome",
    "DecisionReason",
    "DecisionStore",
    "InMemoryDecisionStore",
    "PolicyDecision",
    "SpendPolicyEngine",
    "SpendProposal",
    "SpendUsage",
    "StoredDecision",
    "TreasuryPolicy",
]
