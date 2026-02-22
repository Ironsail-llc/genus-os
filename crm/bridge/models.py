"""Pydantic request/response models for the Bridge API."""

from __future__ import annotations

from pydantic import BaseModel, Field


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


class ImpetusTransmitRequest(BaseModel):
    actingAsProviderId: str | None = None
    confirmationId: str | None = None
