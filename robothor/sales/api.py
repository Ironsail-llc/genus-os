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
from robothor.sales.calibration import Calibration
from robothor.sales.library import (  # noqa: TC001 — FastAPI resolves annotations.
    LibraryPacket,
    preview,
)
from robothor.sales.models import Contract, Draft, QualificationPolicy, SalesSettings
from robothor.sales.recovery import Recovery, RecoveryChange  # noqa: TC001
from robothor.sales.requests import RequestChange, Requests, ResearchRequest  # noqa: TC001
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
            "discovery_mode",
            "review_backlog_limit",
            "mailbox_daily_limit",
            "followup_delays_business_days",
            "discovery_start_hour",
            "discovery_end_hour",
            "timezone",
        }
        if not changes.keys() <= allowed:
            raise ValueError("Only pilot limits and discovery hours can change in this review")
        return changes


class LibrarySelection(ReadRepair):
    expected_revision: int = Field(ge=0, strict=True)
    policy_versions: dict[str, str]
    knowledge_version: str = Field(max_length=80)


class LibraryPublication(ReadRepair):
    packet: LibraryPacket
    expected_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class CalibrationCreate(ReadRepair):
    name: str = Field(min_length=1, max_length=200)
    target_size: int = Field(default=100, ge=1, le=1000, strict=True)
    agreement_target_percent: int = Field(default=85, ge=0, le=100, strict=True)
    expected_settings_revision: int = Field(ge=0, strict=True)


class CalibrationEnrollment(Contract):
    pass


class CalibrationAssessment(ReadRepair):
    reference_decision: Literal["qualified", "rejected", "needs_research"]
    expected_snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_assessment_id: UUID | None


@router.get("")
def overview(request: Request):
    service, _ = require_sales_operator(request)
    return service.overview()


@router.get("/requests")
@domain_errors
def research_requests(request: Request, after: UUID | None = None):
    service, _ = require_sales_operator(request)
    return Requests(service).list(str(after) if after else None)


@router.post("/requests")
@domain_errors
def create_research_request(body: ResearchRequest, request: Request):
    service, actor = require_sales_operator(request)
    return Requests(service).create(body, actor)


@router.get("/requests/{request_id}")
@domain_errors
def research_request(request_id: UUID, request: Request):
    service, _ = require_sales_operator(request)
    return Requests(service).get(str(request_id))


@router.post("/requests/{request_id}/state")
@domain_errors
def change_research_request(request_id: UUID, body: RequestChange, request: Request):
    service, actor = require_sales_operator(request)
    return Requests(service).change(str(request_id), actor=actor, **body.model_dump())


@router.get("/calibration")
@domain_errors
def calibration_cohorts(request: Request, after: UUID | None = None):
    service, _ = require_sales_operator(request)
    return Calibration(service).list_cohorts(after=str(after) if after else None)


@router.post("/calibration")
@domain_errors
def create_calibration(body: CalibrationCreate, request: Request):
    service, actor = require_sales_operator(request)
    return Calibration(service).create(**body.model_dump(), actor=actor)


@router.get("/calibration/{cohort_id}")
@domain_errors
def calibration_report(cohort_id: UUID, request: Request):
    service, _ = require_sales_operator(request)
    return Calibration(service).report(str(cohort_id))


@router.post("/calibration/{cohort_id}/enroll")
@domain_errors
def enroll_calibration(cohort_id: UUID, body: CalibrationEnrollment, request: Request):
    service, actor = require_sales_operator(request)
    return Calibration(service).enroll(str(cohort_id), actor=actor)


@router.get("/calibration/{cohort_id}/items")
@domain_errors
def calibration_items(cohort_id: UUID, request: Request, after: int = Query(default=0, ge=0)):
    service, _ = require_sales_operator(request)
    return Calibration(service).items(str(cohort_id), after=after)


@router.get("/calibration/items/{item_id}")
@domain_errors
def calibration_item(item_id: UUID, request: Request):
    service, _ = require_sales_operator(request)
    return Calibration(service).item(str(item_id))


@router.post("/calibration/items/{item_id}/assessment")
@domain_errors
def assess_calibration(item_id: UUID, body: CalibrationAssessment, request: Request):
    service, actor = require_sales_operator(request)
    return Calibration(service).assess(str(item_id), **body.model_dump(mode="json"), actor=actor)


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


@router.get("/prospects/{prospect_id}/recovery")
@domain_errors
def recovery_state(prospect_id: UUID, request: Request):
    service, _ = require_sales_operator(request)
    return Recovery(service).snapshot(str(prospect_id))


@router.post("/prospects/{prospect_id}/recovery")
@domain_errors
def recover_preparation(prospect_id: UUID, body: RecoveryChange, request: Request):
    service, actor = require_sales_operator(request)
    return Recovery(service).change(str(prospect_id), actor=actor, **body.model_dump())


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


@router.get("/library")
@domain_errors
def library(
    request: Request,
    kind: Literal["qualification", "knowledge"],
    after: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=100, ge=1, le=100),
):
    service, _ = require_sales_operator(request)
    return service.library(kind=kind, after=after, limit=limit)


@router.post("/library/selection")
@domain_errors
def select_library(body: LibrarySelection, request: Request):
    service, actor = require_sales_operator(request)
    service.select_library(**body.model_dump(), actor=actor)
    return service.settings_snapshot()


@router.post("/library/preview")
@domain_errors
def preview_library(body: LibraryPacket, request: Request):
    require_sales_operator(request)
    return preview(body.model_dump(mode="json"))


@router.get("/library/records/{kind}/{version}")
@domain_errors
def library_record(kind: Literal["qualification", "knowledge"], version: str, request: Request):
    service, _ = require_sales_operator(request)
    record = service.library_record(kind=kind, version=version)
    if record is None:
        raise HTTPException(404, detail="Published version not found")
    return record


@router.post("/library/publication")
@domain_errors
def publish_library(body: LibraryPublication, request: Request):
    service, actor = require_sales_operator(request)
    service.publish_library(
        body.packet.model_dump(mode="json"),
        expected_hash=body.expected_hash,
        reason=body.reason,
        actor=actor,
    )
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
