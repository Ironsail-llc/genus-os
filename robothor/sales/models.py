"""Validated contracts shared by agents, integrations and the Helm."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Contract):
    id: str = Field(min_length=1, max_length=100)
    field: str = Field(min_length=1, max_length=100)
    value: str | bool | int
    url: str
    excerpt: str = Field(min_length=1, max_length=4000)
    retrieved_at: datetime
    published_at: datetime | None = None
    expires_at: datetime | None = None
    confidence: Literal["supported", "inferred", "contradicted"] = "supported"

    @field_validator("url")
    @classmethod
    def public_url(cls, value):
        parsed = urlparse(value)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Public source URL required")
        return value

    @field_validator("retrieved_at", "published_at", "expires_at")
    @classmethod
    def aware_date(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Timezone-aware date required")
        return value


class Dossier(Contract):
    buying_case: str = Field(min_length=1, max_length=80)
    evidence: list[Evidence] = Field(default_factory=list, max_length=200)
    criteria: dict[str, list[str]] = Field(default_factory=dict)
    services: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    parent_domain: str | None = None
    unanswered: list[str] = Field(default_factory=list)
    summary: str = Field(default="", max_length=8000)

    @model_validator(mode="after")
    def valid_references(self):
        ids = {e.id for e in self.evidence}
        if len(ids) != len(self.evidence):
            raise ValueError("Evidence IDs must be unique")
        for references in self.criteria.values():
            if not references or not set(references) <= ids:
                raise ValueError("Every criterion needs existing evidence")
        return self


class QualificationPolicy(Contract):
    version: str = Field(min_length=1, max_length=80)
    buying_case: str
    required: list[str]
    weights: dict[str, int]
    threshold: int = Field(ge=0, le=100)
    max_evidence_age_days: int = Field(default=90, ge=1, le=365)

    @model_validator(mode="after")
    def valid_weights(self):
        if any(w < 0 for w in self.weights.values()) or sum(self.weights.values()) != 100:
            raise ValueError("Nonnegative weights must sum to 100")
        if not set(self.required) <= self.weights.keys():
            raise ValueError("Required criteria must be scored")
        return self

    def evaluate(self, dossier: Dossier, now: datetime | None = None) -> dict:
        """Calculate from supported, current evidence; unknown stays unknown."""
        now = now or datetime.now(UTC)
        facts = {e.id: e for e in dossier.evidence}
        supported = []
        missing = []
        disproved = []
        for criterion in self.weights:
            evidence = [facts[i] for i in dossier.criteria.get(criterion, [])]
            current = [
                e
                for e in evidence
                if e.field == criterion
                and e.confidence == "supported"
                and now - timedelta(days=self.max_evidence_age_days) <= e.retrieved_at <= now
                and (e.expires_at is None or e.expires_at > now)
            ]
            yes = any(e.value is True for e in current)
            no = any(e.value is False for e in current)
            if yes and not no:
                supported.append(criterion)
            elif no and not yes:
                disproved.append(criterion)
            else:
                missing.append(criterion)
        score = sum(self.weights[c] for c in supported)
        if dossier.buying_case != self.buying_case:
            decision = "needs_research"
        elif set(disproved) & set(self.required):
            decision = "rejected"
        elif set(missing) & set(self.required):
            decision = "needs_research"
        else:
            decision = "qualified" if score >= self.threshold else "rejected"
        return {
            "policy_version": self.version,
            "buying_case": self.buying_case,
            "score": score,
            "decision": decision,
            "supported": supported,
            "missing": missing,
            "disproved": disproved,
        }


class Contact(Contract):
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    email: str
    source_url: str
    verification: Literal["valid", "invalid", "accept_all", "unknown"] = "unknown"
    verified_at: datetime | None = None

    @field_validator("email")
    @classmethod
    def email_address(cls, value):
        value = value.strip().lower()
        if (
            value.count("@") != 1
            or value.startswith("@")
            or value.endswith("@")
            or any(c.isspace() or c in '<>(),;:"' for c in value)
        ):
            raise ValueError("Single email address required")
        return value


class Draft(Contract):
    recipient: str
    sender: str
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=12000)
    claim_ids: list[str] = Field(min_length=1)
    knowledge_version: str
    evidence_ids: list[str] = Field(min_length=1)
    reply_to_uuid: str | None = None
    purpose: Literal["initial", "followup", "reply", "onboarding"] = "initial"

    @field_validator("recipient", "sender")
    @classmethod
    def address(cls, value):
        return Contact.email_address(value)

    @model_validator(mode="after")
    def threaded(self):
        if self.purpose != "initial" and not self.reply_to_uuid:
            raise ValueError("Follow-ups and replies require an existing thread")
        return self


class Outcome(Contract):
    external_company_id: str = Field(min_length=1, max_length=200)
    event_id: str = Field(min_length=1, max_length=200)
    kind: Literal[
        "signup", "account_ready", "first_order_placed", "order_completed", "order_reversed"
    ]
    occurred_at: datetime
    order_ref: str | None = None

    @field_validator("occurred_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None or value > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("Valid timezone-aware event time required")
        return value


QueueStage = Literal[
    "plan",
    "scout",
    "research",
    "qualify",
    "contacts",
    "verify",
    "promotion",
    "draft",
    "conversation",
    "activation",
    "delivery",
    "stop",
    "inbox",
    "reconcile",
    "business",
]


class DiscoverySegment(Contract):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")
    buying_case: str = Field(min_length=1, max_length=80)
    query: str = Field(min_length=1, max_length=2000)


class BusinessSource(Contract):
    source: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    account_id: str = Field(min_length=1, max_length=200, strict=True)
    refresh_seconds: int = Field(default=21600, ge=600, le=604800, strict=True)


class SalesSettings(Contract):
    fleet_release_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", strict=True)
    research_enabled: StrictBool = False
    enrichment_enabled: StrictBool = False
    promotion_enabled: StrictBool = False
    sending_enabled: StrictBool = False
    outcomes_enabled: StrictBool = False
    business_sources: list[BusinessSource] = Field(default_factory=list, max_length=10)
    review_backlog_limit: int = Field(default=100, ge=1, le=10000, strict=True)
    discovery_daily_limit: int = Field(default=20, ge=0, le=1000, strict=True)
    discovery_segments: list[DiscoverySegment] = Field(default_factory=list, max_length=50)
    discovery_start_hour: int = Field(default=2, ge=0, le=23, strict=True)
    discovery_end_hour: int = Field(default=7, ge=1, le=24, strict=True)
    workflow_bindings: dict[QueueStage, str] = Field(default_factory=dict)
    monthly_limit_units: int = Field(default=0, ge=0, strict=True)
    daily_limit_units: int = Field(default=0, ge=0, strict=True)
    verification_allowance_units: int = Field(default=0, ge=0, le=1_000_000, strict=True)
    mailbox_daily_limit: int = Field(default=5, ge=0, le=100, strict=True)
    senders: list[str] = Field(default_factory=list, max_length=100)
    mailbox_approved_until: dict[str, datetime] = Field(default_factory=dict)
    postal_address: str = Field(default="", max_length=1000)
    unsubscribe_url: str = ""
    timezone: str = "America/Chicago"
    agents: dict[str, str] = Field(default_factory=dict)
    active_knowledge_version: str = ""
    active_policy_versions: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def discovery_configuration(self):
        if self.discovery_start_hour >= self.discovery_end_hour:
            raise ValueError("Discovery window must start before it ends")
        if len({s.id for s in self.discovery_segments}) != len(self.discovery_segments):
            raise ValueError("Discovery segment IDs must be unique")
        if len({s.source for s in self.business_sources}) != len(self.business_sources):
            raise ValueError("Business source names must be unique")
        if any(not value.strip() for value in self.workflow_bindings.values()):
            raise ValueError("Workflow bindings require nonempty native workflow IDs")
        return self

    @field_validator("senders")
    @classmethod
    def addresses(cls, values):
        return [Contact.email_address(v) for v in values]

    @field_validator("timezone")
    @classmethod
    def timezone_name(cls, value):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Valid IANA timezone required") from None
        return value

    @field_validator("unsubscribe_url")
    @classmethod
    def unsubscribe(cls, value):
        return Evidence.public_url(value) if value else value


class Message(Contract):
    provider_id: str = Field(min_length=1, max_length=200)
    prospect_id: str
    direction: Literal["inbound", "outbound"]
    occurred_at: datetime
    sender: str
    recipient: str
    subject: str = Field(max_length=2000)
    body: str = Field(max_length=50000)
    campaign_id: str | None = None
    auto_reply: StrictBool = False
    thread_id: str | None = Field(default=None, max_length=200)

    @field_validator("sender", "recipient")
    @classmethod
    def address(cls, value):
        return Contact.email_address(value)

    @field_validator("occurred_at")
    @classmethod
    def date(cls, value):
        return Outcome.aware(value)


class Candidate(Contract):
    name: str = Field(min_length=1, max_length=300)
    website: str
    source_url: str
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("website", "source_url")
    @classmethod
    def url(cls, value):
        return Evidence.public_url(value)


class CandidateBatch(Contract):
    companies: list[Candidate] = Field(max_length=20)


class ContactBatch(Contract):
    contacts: list[Contact] = Field(max_length=5)

    @model_validator(mode="after")
    def no_verification_claim(self):
        if any(c.verification != "unknown" or c.verified_at is not None for c in self.contacts):
            raise ValueError("Only provider verification can establish deliverability")
        return self


class ConversationDecision(Contract):
    classification: Literal[
        "interested",
        "question",
        "objection",
        "opt_out",
        "wrong_person",
        "out_of_office",
        "complaint",
        "human_required",
    ]
    reason: str = Field(min_length=1, max_length=2000)
    draft: Draft | None = None

    @model_validator(mode="after")
    def response_boundary(self):
        if self.draft and (
            self.classification not in {"interested", "question", "objection"}
            or self.draft.purpose != "reply"
        ):
            raise ValueError("This classification cannot produce an autonomous reply draft")
        return self


class ActivationDecision(Contract):
    next_step: str = Field(min_length=1, max_length=2000)
    human_required: StrictBool
    draft: Draft | None = None

    @model_validator(mode="after")
    def standard_onboarding_only(self):
        if self.draft and (self.human_required or self.draft.purpose != "onboarding"):
            raise ValueError("Only standard onboarding can produce a draft")
        return self
