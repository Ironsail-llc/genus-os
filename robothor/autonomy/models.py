"""Strict public reference, delegation and operation contracts."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from robothor.entity.payments import Identifier

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


class WebOperation(StrictModel):
    origin: str
    action: Action
    purpose: str = Field(min_length=3, max_length=300)
    idempotency_key: str = Field(min_length=6, max_length=128, pattern=r"^[\w.:-]+$")
    amount_minor: Minor = 0
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    recurring_minor: Minor = 0
    annual_commitment_minor: Minor = 0

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
        if self.action == "subscription" and not self.annual_commitment_minor:
            raise ValueError("subscription requires an annual commitment")
        return self


class Delegation(StrictModel):
    enabled: bool = True
    agent_ids: frozenset[Identifier]
    origins: frozenset[str]
    actions: frozenset[Action]
    expires_at: datetime
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    per_purchase_minor: Minor = 0
    monthly_minor: Minor = 0
    recurring_minor: Minor = 0
    annual_minor: Minor = 0
    frame_origins: frozenset[str] = frozenset()

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
            (operation.origin in self.origins, "origin_not_allowed"),
            (operation.action in self.actions, "action_not_allowed"),
            (operation.currency == self.currency, "currency_not_allowed"),
            (operation.amount_minor <= self.per_purchase_minor, "purchase_limit"),
            (
                not spending
                or (used_minor >= 0 and used_minor + operation.amount_minor <= self.monthly_minor),
                "monthly_limit",
            ),
            (operation.recurring_minor <= self.recurring_minor, "recurring_limit"),
            (operation.annual_commitment_minor <= self.annual_minor, "annual_limit"),
        ]
        return next((reason for passed, reason in checks if not passed), "allow")


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
