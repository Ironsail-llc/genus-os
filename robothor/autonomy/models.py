"""Strict public reference, delegation and operation contracts."""

import ipaddress
import re
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_serializer,
    model_validator,
)

from robothor.entity.payments import Identifier
from robothor.entity.spend_limits import DecisionOutcome, DecisionReason, decide_limits

Minor = Annotated[int, Field(strict=True, ge=0, le=10**12)]
Action = Literal["account", "login", "application", "purchase", "subscription"]
Kind = Literal["profile", "credential", "document", "totp", "browser_session", "payment_card"]


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("expected an HTTPS origin without credentials, path or query")
    # Accessing port validates malformed port strings too.
    port = parsed.port
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}" + (f":{port}" if port and port != 443 else "")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Scope(StrictModel):
    tenant_id: Identifier
    owner_id: Identifier


class RequestContext(StrictModel):
    """Attribution supplied by the authenticated execution layer, not the model."""

    run_id: UUID
    actor_id: Identifier


class ResourceInput(StrictModel):
    kind: Kind
    label: str = Field(min_length=1, max_length=100)
    origin: str | None = None
    payload: SecretStr = Field(repr=False, max_length=8_000_000)

    @field_validator("origin")
    @classmethod
    def valid_origin(cls, value: str | None) -> str | None:
        return origin(value) if value else None


class PaymentCard(StrictModel):
    number: SecretStr = Field(repr=False)
    expiry_month: int = Field(strict=True, ge=1, le=12)
    expiry_year: int = Field(strict=True, ge=2026, le=2100)
    name: SecretStr = Field(repr=False)

    @field_validator("number")
    @classmethod
    def valid_number(cls, value: SecretStr) -> SecretStr:
        digits = value.get_secret_value().replace(" ", "").replace("-", "")
        if not digits.isascii() or not digits.isdigit() or not 12 <= len(digits) <= 19:
            raise ValueError("invalid card number")
        total = 0
        for index, char in enumerate(reversed(digits)):
            number = int(char) * (2 if index % 2 else 1)
            total += number - 9 if number > 9 else number
        if total % 10:
            raise ValueError("invalid card number")
        return SecretStr(digits)


class Recurrence(StrictModel):
    interval_months: Literal[1, 2, 3, 6, 12]
    next_charge_on: date
    ends_on: date | None = None

    @model_validator(mode="after")
    def ordered_dates(self) -> "Recurrence":
        if self.ends_on is not None and self.ends_on < self.next_charge_on:
            raise ValueError("recurrence end precedes next charge")
        return self


class WebOperation(StrictModel):
    origin: str
    action: Action
    purpose: str = Field(min_length=3, max_length=300)
    idempotency_key: str = Field(min_length=6, max_length=128, pattern=r"^[\w.:-]+$")
    amount_minor: Minor = 0
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    recurring_minor: Minor = 0
    annual_commitment_minor: Minor = 0
    recurrence: Recurrence | None = None

    @field_validator("origin")
    @classmethod
    def valid_origin(cls, value: str) -> str:
        return origin(value)

    @model_validator(mode="after")
    def commitment_consistent(self) -> "WebOperation":
        if self.action not in ("purchase", "subscription") and (
            self.amount_minor or self.recurring_minor or self.annual_commitment_minor
        ):
            raise ValueError("nonpayment operation cannot spend")
        if self.recurring_minor and self.annual_commitment_minor < self.recurring_minor:
            raise ValueError("annual commitment must cover the recurring payment")
        if self.recurrence and not self.recurring_minor:
            raise ValueError("recurrence requires a recurring amount")
        if self.action == "subscription" and not self.annual_commitment_minor:
            raise ValueError("subscription requires an annual commitment")
        return self


class Delegation(StrictModel):
    enabled: bool = True
    agent_ids: frozenset[Identifier]
    origins: frozenset[str]
    allow_any_website: bool = False
    actions: frozenset[Action]
    expires_at: datetime
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    per_purchase_minor: Minor = 0
    monthly_minor: Minor = 0
    recurring_minor: Minor = 0
    annual_minor: Minor = 0
    frame_origins: frozenset[str] = frozenset()
    allowed_purposes: frozenset[str] = Field(default=frozenset(), max_length=80)
    verification_senders: dict[str, frozenset[str]] = Field(default_factory=dict, max_length=80)

    @model_serializer(mode="wrap")
    def compatible_policy(self, handler: Any) -> dict[str, Any]:
        """Omit a field nobody used, so the row stays readable after a revert.

        The whole policy is persisted as JSONB and read back through this
        StrictModel (``extra="forbid"``). A field that always serialises is a
        one-way upgrade: revert the deployment and every grant created since
        fails ``model_validate`` — not just its new feature, but every
        autonomy operation that has to load the grant first. Same treatment,
        same reason, as ``ExecutionPlan.compatible_plan``.

        A grant that actually USES one of these still writes it, and is
        correctly unreadable by a release that would silently ignore it.
        """
        data: dict[str, Any] = handler(self)
        if not self.allowed_purposes:
            data.pop("allowed_purposes", None)
        if not self.verification_senders:
            data.pop("verification_senders", None)
        return data

    @field_validator("verification_senders")
    @classmethod
    def valid_verification_senders(
        cls, values: dict[str, frozenset[str]]
    ) -> dict[str, frozenset[str]]:
        result = {}
        for destination, domains in values.items():
            destination = origin(destination)
            if destination in result or not 1 <= len(domains) <= 20:
                raise ValueError("invalid or duplicate verification destination")
            normalized = set()
            for domain in domains:
                domain = domain.encode("idna").decode("ascii").lower()
                if (
                    len(domain) > 253
                    or "." not in domain
                    or any(
                        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                        for label in domain.split(".")
                    )
                ):
                    raise ValueError("expected an exact mail sender domain")
                from robothor.autonomy.verification import shared_mail_domain

                if shared_mail_domain(domain):
                    raise ValueError("mail sender must belong to that website alone")
                try:
                    ipaddress.ip_address(domain)
                except ValueError:
                    normalized.add(domain)
                else:
                    raise ValueError("mail sender must be a DNS domain")
            result[destination] = frozenset(normalized)
        return result

    @field_validator("allowed_purposes")
    @classmethod
    def valid_purposes(cls, values: frozenset[str]) -> frozenset[str]:
        cleaned = frozenset(value.strip() for value in values)
        if any(not 3 <= len(value) <= 300 for value in cleaned):
            raise ValueError("purposes must contain 3 to 300 characters")
        return cleaned

    @field_validator("origins", "frame_origins")
    @classmethod
    def valid_origins(cls, values: frozenset[str]) -> frozenset[str]:
        return frozenset(origin(value) for value in values)

    @field_validator("expires_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("expiry requires a timezone")
        return value

    def decision(
        self,
        operation: WebOperation,
        *,
        agent_id: str,
        used_minor: int,
        now: datetime | None = None,
    ) -> str:
        spending = operation.action in {"purchase", "subscription"}
        checks = [
            (self.enabled, "grant_disabled"),
            (self.expires_at > (now or datetime.now(UTC)), "grant_expired"),
            (agent_id in self.agent_ids, "agent_not_allowed"),
            (self.allow_any_website or operation.origin in self.origins, "origin_not_allowed"),
            (operation.action in self.actions, "action_not_allowed"),
            (
                not self.allowed_purposes
                or operation.purpose.strip().casefold()
                in {purpose.casefold() for purpose in self.allowed_purposes},
                "purpose_not_allowed",
            ),
            (operation.currency == self.currency, "currency_not_allowed"),
        ]
        failed = next((reason for passed, reason in checks if not passed), None)
        if failed:
            return failed
        outcome, reason = decide_limits(
            amount=operation.amount_minor,
            per_transaction_limit=self.per_purchase_minor,
            monthly_limit=self.monthly_minor,
            monthly_used=used_minor if spending else 0,
        )
        if outcome != DecisionOutcome.ALLOW:
            return (
                "purchase_limit"
                if reason == DecisionReason.PER_TRANSACTION_LIMIT
                else "monthly_limit"
            )
        if operation.recurring_minor > self.recurring_minor:
            return "recurring_limit"
        if operation.annual_commitment_minor > self.annual_minor:
            return "annual_limit"
        return "allow"


class RuntimeSettings(StrictModel):
    enabled: bool = False
    managed_browser: bool = False
    payment_processing: bool = False
    payment_assessment_reference: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def assessed_payments(self) -> "RuntimeSettings":
        if self.payment_processing and not self.payment_assessment_reference.strip():
            raise ValueError("payment processing requires a deployment assessment reference")
        return self
