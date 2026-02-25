"""Pydantic request/response models for the Bridge API."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _check_uuid(v: str | None, field_name: str) -> str | None:
    """Validate that a value is a valid UUID or None."""
    if v is None:
        return v
    if not _UUID_RE.match(v):
        raise ValueError(f"{field_name} must be a valid UUID, got: {v[:60]!r}")
    return v


# ─── People ──────────────────────────────────────────────────────────────


class CreatePersonRequest(BaseModel):
    firstName: str
    lastName: str = ""
    email: str | None = None
    phone: str | None = None


class UpdatePersonRequest(BaseModel):
    firstName: str | None = None
    lastName: str | None = None
    email: str | None = None
    phone: str | None = None
    jobTitle: str | None = None
    companyId: str | None = None
    city: str | None = None
    linkedinUrl: str | None = None
    avatarUrl: str | None = None


class MergeRequest(BaseModel):
    primaryId: str | None = None
    secondaryId: str | None = None
    keeper_id: str | None = None
    loser_id: str | None = None

    def get_ids(self) -> tuple[str | None, str | None]:
        keeper = self.primaryId or self.keeper_id
        loser = self.secondaryId or self.loser_id
        return keeper, loser


# ─── Companies ───────────────────────────────────────────────────────────


class UpdateCompanyRequest(BaseModel):
    name: str | None = None
    domainName: str | None = None
    employees: int | None = None
    address: str | None = None
    linkedinUrl: str | None = None
    idealCustomerProfile: bool | None = None


# ─── Notes ───────────────────────────────────────────────────────────────


class CreateNoteRequest(BaseModel):
    title: str
    body: str = ""
    personId: str | None = None
    companyId: str | None = None

    @field_validator("personId", "companyId", mode="before")
    @classmethod
    def validate_uuids(cls, v: str | None, info: object) -> str | None:
        return _check_uuid(v, info.field_name)


# ─── Tasks ──────────────────────────────────────────────────────────────


class CreateTaskRequest(BaseModel):
    title: str
    body: str | None = None
    status: str = "TODO"
    dueAt: str | None = None
    personId: str | None = None
    companyId: str | None = None
    assignedToAgent: str | None = None
    priority: str = "normal"
    tags: list[str] | None = None
    parentTaskId: str | None = None

    @field_validator("assignedToAgent", mode="before")
    @classmethod
    def normalize_assigned_to_agent(cls, v: str | None) -> str | None:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("personId", "companyId", "parentTaskId", mode="before")
    @classmethod
    def validate_uuids(cls, v: str | None, info: object) -> str | None:
        return _check_uuid(v, info.field_name)


class UpdateTaskRequest(BaseModel):
    title: str | None = None
    body: str | None = None
    status: str | None = None
    dueAt: str | None = None
    personId: str | None = None
    companyId: str | None = None
    assignedToAgent: str | None = None
    priority: str | None = None
    tags: list[str] | None = None
    parentTaskId: str | None = None
    resolution: str | None = None

    @field_validator("assignedToAgent", mode="before")
    @classmethod
    def normalize_assigned_to_agent(cls, v: str | None) -> str | None:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("personId", "companyId", "parentTaskId", mode="before")
    @classmethod
    def validate_uuids(cls, v: str | None, info: object) -> str | None:
        return _check_uuid(v, info.field_name)


# ─── Routines ────────────────────────────────────────────────────────────


class CreateRoutineRequest(BaseModel):
    title: str
    cronExpr: str
    body: str | None = None
    timezone: str = "America/New_York"
    assignedToAgent: str | None = None
    priority: str = "normal"
    tags: list[str] | None = None
    personId: str | None = None
    companyId: str | None = None

    @field_validator("personId", "companyId", mode="before")
    @classmethod
    def validate_uuids(cls, v: str | None, info: object) -> str | None:
        return _check_uuid(v, info.field_name)


class UpdateRoutineRequest(BaseModel):
    title: str | None = None
    body: str | None = None
    cronExpr: str | None = None
    timezone: str | None = None
    assignedToAgent: str | None = None
    priority: str | None = None
    tags: list[str] | None = None
    active: bool | None = None
    personId: str | None = None
    companyId: str | None = None


# ─── Messages ────────────────────────────────────────────────────────────


class CreateMessageRequest(BaseModel):
    content: str
    message_type: str = "outgoing"
    private: bool = False


class ToggleStatusRequest(BaseModel):
    status: str = "resolved"


# ─── Integration ─────────────────────────────────────────────────────────


class ResolveContactRequest(BaseModel):
    channel: str
    identifier: str
    name: str | None = None


class LogInteractionRequest(BaseModel):
    contact_name: str
    channel: str = "api"
    direction: str = "outgoing"
    content_summary: str = ""
    channel_identifier: str | None = None


class WebhookRequest(BaseModel):
    channel: str = "unknown"
    identifier: str = ""
    name: str = ""
    content: str = ""
    direction: str = "incoming"


# ─── Memory ──────────────────────────────────────────────────────────────


class MemorySearchRequest(BaseModel):
    query: str
    limit: int = Field(default=10, ge=1, le=100)


class MemoryStoreRequest(BaseModel):
    content: str
    content_type: str = "conversation"


class MemoryBlockWriteRequest(BaseModel):
    content: str


# ─── Vault ───────────────────────────────────────────────────────────────


class VaultCreateLoginRequest(BaseModel):
    name: str
    username: str
    password: str
    uri: str | None = None
    notes: str | None = None


class VaultCreateCardRequest(BaseModel):
    name: str
    number: str
    expMonth: str
    expYear: str
    cardholderName: str = ""
    code: str | None = None
    brand: str | None = None
    notes: str | None = None


# ─── Impetus ─────────────────────────────────────────────────────────────


# ─── Tasks (Review Workflow) ────────────────────────────────────────────


class ApproveTaskRequest(BaseModel):
    resolution: str = ""


class RejectTaskRequest(BaseModel):
    reason: str
    changeRequests: list[str] | None = None


# ─── Notifications ──────────────────────────────────────────────────────


class SendNotificationRequest(BaseModel):
    fromAgent: str
    toAgent: str
    notificationType: str = "info"
    subject: str
    body: str | None = None
    metadata: dict | None = None
    taskId: str | None = None

    @field_validator("taskId", mode="before")
    @classmethod
    def validate_uuids(cls, v: str | None, info: object) -> str | None:
        return _check_uuid(v, info.field_name)


# ─── Tenants ────────────────────────────────────────────────────────────


class CreateTenantRequest(BaseModel):
    id: str
    displayName: str
    parentTenantId: str | None = None
    settings: dict | None = None


class UpdateTenantRequest(BaseModel):
    displayName: str | None = None
    parentTenantId: str | None = None
    settings: dict | None = None
    active: bool | None = None


# ─── Memory (Append) ───────────────────────────────────────────────────


class MemoryBlockAppendRequest(BaseModel):
    entry: str
    maxEntries: int = Field(default=20, ge=1, le=100)


# ─── Impetus ─────────────────────────────────────────────────────────


class ImpetusTransmitRequest(BaseModel):
    actingAsProviderId: str | None = None
    confirmationId: str | None = None
