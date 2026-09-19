"""Tenant-scoped human sales console API; never an agent approval surface.

Synchronous routes run database work in FastAPI's thread pool. Authentication
is supplied by the bridge, and no request body may select a tenant or actor.
"""

from __future__ import annotations

from functools import wraps
from typing import Literal
from uuid import UUID  # noqa: TC003 — FastAPI resolves this annotation at runtime.

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field, StrictBool, ValidationError, field_validator

from robothor.operations.store import Conflict
from robothor.sales.business_repair import (
    Reassignment,  # noqa: TC001 — FastAPI resolves at runtime.
)
from robothor.sales.models import Contract, Draft, QualificationPolicy, SalesSettings
from robothor.sales.service import Sales

router = APIRouter(prefix="/api/sales", tags=["sales"])


def require_sales_operator(request: Request):
    auth = getattr(request.state, "auth", None)
    if auth is None or auth.is_service or auth.role not in {"owner", "admin"} or not auth.tenant_id:
        raise HTTPException(status_code=403, detail="Human tenant operator required")
    return Sales(auth.tenant_id), f"operator:{auth.actor_id}"


def domain_errors(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Conflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValidationError:
            raise HTTPException(status_code=422, detail="Invalid sales contract") from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    return wrapped


class Decision(Contract):
    approved: StrictBool
    note: str = Field(default="", max_length=4000)


class ProspectDecision(Decision):
    expected_version: int = Field(ge=0, strict=True)
    expected_policy_version: str | None


class Discovery(Contract):
    name: str
    website: str
    source_url: str


class Knowledge(Contract):
    version: str
    data: dict


class Suppression(Contract):
    email: str
    reason: str = Field(min_length=1, max_length=1000)


class Customer(Contract):
    external_company_id: str = Field(min_length=1, max_length=200)


class BusinessCustomer(Contract):
    observation_id: UUID
    expected_revision: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=10, max_length=2000)


class ReadRepair(Contract):
    reason: str = Field(min_length=10, max_length=2000)


class SettingsReview(ReadRepair):
    expected_revision: int = Field(ge=0, strict=True)
    changes: dict = Field(min_length=1)

    @field_validator("changes")
    @classmethod
    def pilot_limits_only(cls, changes):
        allowed = {
            "monthly_limit_units",
            "daily_limit_units",
            "verification_allowance_units",
            "discovery_daily_limit",
            "review_backlog_limit",
            "mailbox_daily_limit",
            "discovery_start_hour",
            "discovery_end_hour",
            "timezone",
        }
        if not changes.keys() <= allowed:
            raise ValueError("Only pilot limits and discovery hours can change in this review")
        return changes


@router.get("")
def overview(request: Request):
    service, _ = require_sales_operator(request)
    return service.overview()


@router.post("/jobs/{job_id}/retry")
@domain_errors
def retry_read(job_id: UUID, body: ReadRepair, request: Request):
    service, actor = require_sales_operator(request)
    service.retry_provider_read(str(job_id), actor, body.reason)
    return {"ok": True}


@router.get("/provider-reads")
@domain_errors
def provider_reads(
    request: Request,
    state: Literal["attention", "all"] = "attention",
    kind: Literal["sales.inbound", "sales.reconcile", "sales.business"] | None = None,
    after: UUID | None = None,
):
    service, _ = require_sales_operator(request)
    return service.provider_reads(state=state, kind=kind, after=str(after) if after else None)


@router.patch("/settings")
@domain_errors
def configure(body: SalesSettings, request: Request):
    service, actor = require_sales_operator(request)
    service.configure(body.model_dump(exclude_unset=True), actor)
    return service.settings()


@router.get("/settings")
@domain_errors
def settings_snapshot(request: Request):
    service, _ = require_sales_operator(request)
    return service.settings_snapshot()


@router.post("/settings/review")
@domain_errors
def review_settings(body: SettingsReview, request: Request):
    service, actor = require_sales_operator(request)
    service.configure(
        body.changes, actor, expected_revision=body.expected_revision, reason=body.reason
    )
    return service.settings_snapshot()


@router.post("/prospects")
@domain_errors
def discover(body: Discovery, request: Request):
    service, _ = require_sales_operator(request)
    return service.discover(**body.model_dump())


@router.get("/prospects/{prospect_id}")
@domain_errors
def dossier(prospect_id: str, request: Request):
    service, _ = require_sales_operator(request)
    prospect = service.get(prospect_id)
    if not prospect:
        raise HTTPException(status_code=404, detail="Prospect not found")
    return {
        "prospect": prospect,
        "contacts": service.contacts(prospect_id),
        "messages": service.messages(prospect_id),
        "history": service.history(prospect_id),
        "retention": service.retention(prospect_id),
    }


@router.post("/prospects/{prospect_id}/review")
@domain_errors
def review(prospect_id: str, body: ProspectDecision, request: Request):
    service, actor = require_sales_operator(request)
    service.accept(
        prospect_id,
        body.approved,
        actor,
        body.note,
        expected_version=body.expected_version,
        expected_policy_version=body.expected_policy_version,
    )
    return {"ok": True}


@router.post("/prospects/{prospect_id}/drafts")
@domain_errors
def draft(prospect_id: str, body: Draft, request: Request):
    service, _ = require_sales_operator(request)
    return {"action_id": service.draft(prospect_id, body)}


@router.post("/actions/{action_id}/decision")
@domain_errors
def decide(action_id: str, body: Decision, request: Request):
    service, actor = require_sales_operator(request)
    service.ops.decide(action_id, body.approved, actor)
    return {"ok": True}


@router.post("/prospects/{prospect_id}/takeover")
@domain_errors
def takeover(prospect_id: str, request: Request):
    service, actor = require_sales_operator(request)
    service.takeover(prospect_id, actor)
    return {"ok": True}


@router.post("/prospects/{prospect_id}/customer")
@domain_errors
def bind_customer(prospect_id: str, body: Customer, request: Request):
    service, actor = require_sales_operator(request)
    service.bind_customer(prospect_id, body.external_company_id, actor)
    return {"ok": True}


@router.post("/prospects/{prospect_id}/business-customer")
@domain_errors
def bind_business_customer(prospect_id: UUID, body: BusinessCustomer, request: Request):
    service, actor = require_sales_operator(request)
    service.bind_business_customer(
        str(prospect_id), str(body.observation_id), body.expected_revision, actor, body.reason
    )
    return {"ok": True}


@router.get("/business-observations")
@domain_errors
def business_observations(
    request: Request,
    kind: Literal["practice", "signup", "order"] = "practice",
    source: str | None = Query(default=None, pattern=r"^[a-z][a-z0-9_-]{0,39}$"),
    account_id: str | None = Query(default=None, min_length=1, max_length=200),
    after: UUID | None = None,
):
    service, _ = require_sales_operator(request)
    return service.business_records(
        kind=kind, source=source, account_id=account_id, after=str(after) if after else None
    )


@router.post("/business-observations/{observation_id}/reassign")
@domain_errors
def reassign_business_customer(observation_id: UUID, body: Reassignment, request: Request):
    service, actor = require_sales_operator(request)
    service.reassign_business_customer(
        str(observation_id), actor=actor, **body.model_dump(mode="json")
    )
    return {"ok": True}


@router.post("/suppression")
@domain_errors
def suppress(body: Suppression, request: Request):
    service, actor = require_sales_operator(request)
    service.suppress(body.email, body.reason, actor)
    return {"ok": True}


@router.post("/policies")
@domain_errors
def policy(body: QualificationPolicy, request: Request):
    service, actor = require_sales_operator(request)
    service.publish_policy(body, actor)
    return {"ok": True}


@router.post("/knowledge")
@domain_errors
def knowledge(body: Knowledge, request: Request):
    service, actor = require_sales_operator(request)
    service.publish_knowledge(body.version, body.data, actor)
    return {"ok": True}
