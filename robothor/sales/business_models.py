"""Minimized current business records. Raw provider payloads never enter storage."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from robothor.sales.models import Contact, Contract, Outcome

Identity = Annotated[str, Field(strict=True, min_length=1, max_length=200)]


class BusinessRecord(Contract):
    source_updated_at: datetime

    @field_validator("source_updated_at")
    @classmethod
    def valid_time(cls, value):
        return Outcome.aware(value)


class PracticeRecord(BusinessRecord):
    business_unit_id: Identity
    name: str = Field(min_length=1, max_length=1000)
    state: str | None = Field(default=None, max_length=100)
    active: StrictBool
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def valid_creation(cls, value):
        return Outcome.aware(value)


class CrmCandidates(Contract):
    source_account_ref: Identity
    verified_at: datetime
    organization_id: StrictInt | None = Field(default=None, gt=0)
    person_id: StrictInt = Field(gt=0)
    deal_id: StrictInt = Field(gt=0)

    @field_validator("verified_at")
    @classmethod
    def valid_time(cls, value):
        return Outcome.aware(value)


class SignupRecord(BusinessRecord):
    source_user_id: Identity
    email: str
    name: str = Field(max_length=1000)
    state: str | None = Field(default=None, max_length=100)
    signed_up_at: datetime
    onboarding_status: str = Field(min_length=1, max_length=100)
    lifecycle_status: str = Field(min_length=1, max_length=100)
    practice_id: Identity | None = None
    business_unit_id: Identity | None = None
    approved_at: datetime | None = None
    account_ready: StrictBool
    crm_candidates: CrmCandidates | None = None

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        return Contact.email_address(value)

    @field_validator("signed_up_at", "approved_at")
    @classmethod
    def valid_time(cls, value):
        return Outcome.aware(value) if value else None

    @model_validator(mode="after")
    def consistent(self):
        if (self.practice_id is None) != (self.business_unit_id is None):
            raise ValueError("Complete business identity required")
        if self.approved_at and self.approved_at < self.signed_up_at:
            raise ValueError("Approval precedes signup")
        if self.account_ready and (not self.practice_id or not self.approved_at):
            raise ValueError("Account readiness evidence required")
        return self


class OrderRecord(BusinessRecord):
    practice_id: Identity
    category: str = Field(min_length=1, max_length=80)
    placed_at: datetime | None
    fulfilled_at: datetime | None
    fulfillment: Literal["verified", "not_verified", "unknown", "excluded"]

    @field_validator("placed_at", "fulfilled_at")
    @classmethod
    def valid_time(cls, value):
        return Outcome.aware(value) if value else None

    @model_validator(mode="after")
    def consistent(self):
        if self.fulfillment == "verified":
            if not self.placed_at or not self.fulfilled_at or self.fulfilled_at < self.placed_at:
                raise ValueError("Coherent fulfillment evidence required")
        elif self.fulfilled_at is not None:
            raise ValueError("Unverified fulfillment timestamp")
        return self


class ObservationKey(Contract):
    source: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    account_id: Identity
    kind: Literal["practice", "signup", "order"]
    external_id: Identity
    revision: Identity
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def valid_time(cls, value):
        return Outcome.aware(value)


class BusinessScan(Contract):
    source: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    account_id: Identity
    kind: Literal["practice", "signup", "order"]
    practice_id: Identity | None
    after: Identity | None
    seen_cursors: list[Identity] = Field(max_length=1000)
    scan_id: Identity

    @model_validator(mode="after")
    def scope(self):
        if (self.kind == "order") != (self.practice_id is not None):
            raise ValueError("Order pages require an explicit practice scope")
        return self


class BusinessPageItem(Contract):
    external_id: Identity
    revision: Identity
    data: dict[str, Any]


class HistoryCoverage(Contract):
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    total: StrictInt = Field(ge=0, le=10000)
    through: datetime

    @field_validator("through")
    @classmethod
    def valid_time(cls, value):
        return Outcome.aware(value)


class BusinessPage(Contract):
    source: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    account_id: Identity
    kind: Literal["practice", "signup", "order"]
    practice_id: Identity | None
    after: Identity | None
    observed_at: datetime
    next_cursor: Identity | None
    items: list[BusinessPageItem] = Field(max_length=100)
    coverage: HistoryCoverage | None = None

    @field_validator("observed_at")
    @classmethod
    def valid_time(cls, value):
        return Outcome.aware(value)

    @model_validator(mode="after")
    def unique(self):
        if self.coverage and (self.kind != "order" or self.coverage.through > self.observed_at):
            raise ValueError("Order coverage must precede observation")
        ids = [item.external_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate business observation identity")
        return self
