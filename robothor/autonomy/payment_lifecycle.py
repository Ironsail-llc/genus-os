"""Personal payment evidence projection, separate from execution and budget state.

Only trusted broker/issuer adapters may produce these facts. ``source`` describes
provenance; it is not authentication and must never be accepted from an agent or
an unauthenticated callback. A merchant confirmation cannot establish a charge.
Refunds here do not automatically release a reservation or recurring commitment.
"""

from dataclasses import dataclass
from datetime import date  # noqa: TC003 -- Pydantic field type
from typing import Literal

from pydantic import Field, model_validator

from robothor.autonomy.models import StrictModel


class PaymentFact(StrictModel):
    event_key: str = Field(min_length=1, max_length=128)
    kind: Literal["submitted", "authorized", "charged", "refunded", "reversed"]
    amount_minor: int = Field(ge=0, le=10**12, strict=True)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    source: Literal["merchant", "issuer"]
    renewal_id: str | None = Field(default=None, min_length=1, max_length=128, repr=False)
    renewal_on: date | None = None

    @model_validator(mode="after")
    def renewal_identity(self) -> "PaymentFact":
        if (self.renewal_id is None) != (self.renewal_on is None):
            raise ValueError("renewal_identity_and_date_required")
        if self.renewal_id is not None and self.source != "issuer":
            raise ValueError("issuer_renewal_evidence_required")
        return self


@dataclass(slots=True)
class PaymentPosition:
    state: Literal[
        "unconfirmed",
        "submitted",
        "authorized",
        "charged",
        "partially_refunded",
        "refunded",
        "reversed",
    ] = "unconfirmed"
    authorized_minor: int = 0
    charged_minor: int = 0
    refunded_minor: int = 0
    reversed_minor: int = 0
    limit_exceeded: bool = False
    authorization_exceeded: bool = False

    @property
    def net_charged_minor(self) -> int:
        return self.charged_minor - self.refunded_minor


def _apply(position: PaymentPosition, fact: PaymentFact) -> None:
    amount = fact.amount_minor
    if fact.kind == "submitted":
        if amount:
            raise ValueError("submission_is_not_money_movement")
        if position.state == "unconfirmed":
            position.state = "submitted"
        return
    if fact.source != "issuer":
        raise ValueError("issuer_evidence_required")
    if not amount:
        raise ValueError("positive_payment_amount_required")
    if fact.kind == "authorized":
        if position.authorized_minor:
            raise ValueError("authorization_already_recorded")
        position.authorized_minor = amount
        if not position.charged_minor:
            position.state = "authorized"
    elif fact.kind == "charged":
        if position.reversed_minor:
            raise ValueError("charge_after_reversal")
        total = position.charged_minor + amount
        position.charged_minor = total
        position.state = "partially_refunded" if position.refunded_minor else "charged"
    elif fact.kind == "refunded":
        total = position.refunded_minor + amount
        if total > position.charged_minor:
            raise ValueError("refund_exceeds_charge")
        position.refunded_minor = total
        position.state = "refunded" if total == position.charged_minor else "partially_refunded"
    else:
        if position.charged_minor or position.reversed_minor or amount != position.authorized_minor:
            raise ValueError("invalid_authorization_reversal")
        position.reversed_minor = amount
        position.state = "reversed"


def project_payment(
    facts: list[PaymentFact], *, limit_minor: int, currency: str = "USD"
) -> PaymentPosition:
    if type(limit_minor) is not int or limit_minor < 0:
        raise ValueError("invalid_payment_limit")
    position = PaymentPosition()
    seen: dict[str, PaymentFact] = {}
    for original in facts:
        # model_construct/model_copy bypass validation; never trust those inputs.
        fact = PaymentFact.model_validate(original.model_dump())
        if fact.currency != currency:
            raise ValueError("payment_currency_mismatch")
        previous = seen.get(fact.event_key)
        if previous is not None:
            if previous != fact:
                raise ValueError("payment_event_conflict")
            continue
        seen[fact.event_key] = fact
    # Delivery order is not financial causality. Project the known evidence in
    # dependency order; the encrypted journal still preserves arrival order.
    # Refunds require known captures, and reversals require an authorization.
    # Conflicting captures/reversals remain invalid whichever arrived first.
    if len({(fact.renewal_id, fact.renewal_on) for fact in seen.values()}) > 1:
        raise ValueError("payment_group_mismatch")
    priority = {"submitted": 0, "authorized": 1, "charged": 2, "reversed": 2, "refunded": 3}
    for fact in sorted(seen.values(), key=lambda value: priority[value.kind]):
        _apply(position, fact)
    position.limit_exceeded = max(position.authorized_minor, position.charged_minor) > limit_minor
    position.authorization_exceeded = bool(
        position.authorized_minor and position.charged_minor > position.authorized_minor
    )
    return position
