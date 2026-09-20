"""Personal onboarding, authority and operation status. Secrets travel inward only."""

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr

from robothor.autonomy.broker import ExecutionPlan
from robothor.autonomy.enrollment import EnrollmentRequest, EnrollmentStore
from robothor.autonomy.identity import scope_for_actor
from robothor.autonomy.models import Delegation, ResourceInput, RuntimeSettings, StrictModel
from robothor.autonomy.onboarding import import_contact_profile
from robothor.autonomy.runtime import run_browser
from robothor.autonomy.store import AutonomyStore, declared_plan

router = APIRouter(prefix="/api/autonomy", tags=["autonomy"])
_resumes: set[asyncio.Task] = set()


class VerificationInput(StrictModel):
    verification_code: SecretStr


class EnrollmentToken(StrictModel):
    token: SecretStr


class EnrollmentCompletion(EnrollmentToken):
    resource: ResourceInput


async def require_personal_owner(request: Request):
    auth = getattr(request.state, "auth", None)
    if (
        not auth
        or auth.is_service
        or auth.role not in {"owner", "admin", "member", "user"}
        or not auth.actor_id
        or not auth.tenant_id
    ):
        raise HTTPException(403, "verified personal account required")
    try:
        return await asyncio.to_thread(scope_for_actor, auth.tenant_id, auth.actor_id)
    except PermissionError:
        raise HTTPException(409, "Link your account identity before personal enrollment") from None


async def _body(request: Request, model, invalid="Invalid enrollment data; no values were stored"):
    # Do not use automatic FastAPI model parsing here: validation responses
    # otherwise echo rejected input (including a pasted card or password).
    try:
        raw = await request.body()
        if len(raw) > 8_100_000:
            raise ValueError
        return model.model_validate(json.loads(raw))
    except Exception:
        raise HTTPException(422, invalid) from None


def _safe(value):
    return JSONResponse(value, headers={"Cache-Control": "no-store"})


@router.get("/status")
async def status(request: Request):
    scope = await require_personal_owner(request)
    store = AutonomyStore()
    try:
        return _safe(
            {
                "settings": (await asyncio.to_thread(store.settings, scope)).model_dump(),
                "resources": await asyncio.to_thread(store.resources, scope),
                "grants": await asyncio.to_thread(store.grants, scope),
                "spending": await asyncio.to_thread(store.spending_projection, scope),
            }
        )
    except Exception:
        raise HTTPException(503, "Personal automation storage is not ready") from None


@router.post("/resources")
async def enroll(request: Request):
    scope = await require_personal_owner(request)
    body = await _body(request, ResourceInput)
    try:
        return _safe(await asyncio.to_thread(AutonomyStore().put_resource, scope, body))
    except ValueError:
        raise HTTPException(422, "Invalid resource; no values were stored") from None
    except Exception:
        raise HTTPException(503, "Resource storage unavailable") from None


@router.post("/profile-from-contact")
async def profile_from_contact(request: Request):
    scope = await require_personal_owner(request)
    await _body(request, StrictModel)  # no caller-supplied person or tenant IDs
    try:
        return _safe(await asyncio.to_thread(import_contact_profile, AutonomyStore(), scope))
    except (PermissionError, ValueError):
        raise HTTPException(409, "Your linked contact has no information to import") from None
    except Exception:
        raise HTTPException(503, "Contact import is unavailable") from None


@router.post("/enrollments")
async def enrollment_create(request: Request):
    scope = await require_personal_owner(request)
    body = await _body(request, EnrollmentRequest)
    try:
        return _safe(await asyncio.to_thread(EnrollmentStore(AutonomyStore()).create, scope, body))
    except Exception:
        raise HTTPException(503, "Secure enrollment is unavailable") from None


@router.post("/enrollments/inspect")
async def enrollment_inspect(request: Request):
    scope = await require_personal_owner(request)
    body = await _body(request, EnrollmentToken)
    try:
        return _safe(
            await asyncio.to_thread(
                EnrollmentStore(AutonomyStore()).inspect, scope, body.token.get_secret_value()
            )
        )
    except PermissionError:
        raise HTTPException(404, "Enrollment expired or unavailable for this account") from None
    except Exception:
        raise HTTPException(503, "Secure enrollment is unavailable") from None


@router.post("/enrollments/complete")
async def enrollment_complete(request: Request):
    scope = await require_personal_owner(request)
    body = await _body(request, EnrollmentCompletion)
    try:
        return _safe(
            await asyncio.to_thread(
                EnrollmentStore(AutonomyStore()).complete,
                scope,
                body.token.get_secret_value(),
                body.resource,
            )
        )
    except PermissionError:
        raise HTTPException(404, "Enrollment expired or unavailable for this account") from None
    except ValueError:
        raise HTTPException(422, "Invalid resource; no values were stored") from None
    except Exception:
        raise HTTPException(503, "Secure enrollment is unavailable") from None


@router.post("/resources/refresh-descriptions")
async def refresh_descriptions(request: Request):
    scope = await require_personal_owner(request)
    await _body(request, StrictModel)
    try:
        count = await asyncio.to_thread(AutonomyStore().refresh_resource_descriptors, scope)
        return _safe({"updated": count})
    except Exception:
        raise HTTPException(503, "Saved information could not be checked") from None


@router.delete("/resources/{resource_id}")
async def revoke_resource(resource_id: UUID, request: Request):
    scope = await require_personal_owner(request)
    try:
        await asyncio.to_thread(AutonomyStore().revoke_resource, scope, str(resource_id))
    except PermissionError:
        raise HTTPException(404, "Resource not found") from None
    return _safe({"revoked": True})


@router.post("/grants")
async def delegate(request: Request):
    scope = await require_personal_owner(request)
    policy = await _body(
        request,
        Delegation,
        "Invalid authority; no authority was granted. Each verification sender must be a"
        " mail domain that belongs to that website alone, never a shared mail provider.",
    )
    return _safe(await asyncio.to_thread(AutonomyStore().create_grant, scope, policy))


@router.delete("/grants/{grant_id}")
async def revoke_grant(grant_id: UUID, request: Request):
    scope = await require_personal_owner(request)
    try:
        await asyncio.to_thread(AutonomyStore().revoke_grant, scope, str(grant_id))
    except PermissionError:
        raise HTTPException(404, "Grant not found") from None
    return _safe({"revoked": True})


@router.put("/settings")
async def configure(request: Request):
    scope = await require_personal_owner(request)
    settings = await _body(request, RuntimeSettings)
    # Payment processing posture is a deployment operation, not a grant a
    # regular account can give itself. Owners/admins use the same endpoint.
    if request.state.auth.role not in {"owner", "admin"}:
        previous = await asyncio.to_thread(AutonomyStore().settings, scope)
        if (
            settings.payment_processing != previous.payment_processing
            or settings.payment_assessment_reference != previous.payment_assessment_reference
        ):
            raise HTTPException(403, "Payment deployment settings require an administrator")
    await asyncio.to_thread(AutonomyStore().configure, scope, settings)
    return _safe(settings.model_dump())


@router.get("/operations")
async def operations(request: Request):
    scope = await require_personal_owner(request)
    rows = await asyncio.to_thread(AutonomyStore().recent_operations, scope)
    # datetime encoding is handled without accepting arbitrary stored values.
    for row in rows:
        for key in ("created_at", "updated_at"):
            row[key] = row[key].isoformat()
    from robothor.autonomy.handoffs import HandoffStore

    handoffs = await asyncio.to_thread(HandoffStore(AutonomyStore()).list, scope)
    return _safe({"operations": rows, "handoffs": handoffs})


@router.post("/operations/{operation_id}/verification")
async def verification(operation_id: UUID, request: Request):
    scope = await require_personal_owner(request)
    body = await _body(request, VerificationInput)
    import re

    code = body.verification_code.get_secret_value()
    if not re.fullmatch(r"(?:\d{3,4}|\d{6})", code):
        raise HTTPException(422, "Invalid verification code")
    try:
        row = await asyncio.to_thread(AutonomyStore().resume_with_code, scope, str(operation_id))
        plan = ExecutionPlan.model_validate(declared_plan(row["execution_plan"]))
    except Exception:
        raise HTTPException(409, "Operation is not waiting for this verification") from None

    async def resume():
        try:
            await run_browser(
                scope, str(operation_id), row["agent_id"], plan, verification_code=code
            )
        except Exception:
            # Journal state survives interruption; never log the request or
            # browser exception, both of which can contain the code.
            return

    task = asyncio.create_task(resume())
    _resumes.add(task)
    task.add_done_callback(_resumes.discard)
    return _safe({"operation_id": str(operation_id), "state": "resuming"})


@router.get("/operations/{operation_id}/terms")
async def terms_history(operation_id: UUID, request: Request):
    from robothor.autonomy.terms_audit import TermsAudit

    scope = await require_personal_owner(request)
    try:
        rows = await asyncio.to_thread(TermsAudit(AutonomyStore()).list, scope, str(operation_id))
        return _safe({"snapshots": rows})
    except PermissionError:
        raise HTTPException(404, "Submission record not found") from None
    except Exception:
        raise HTTPException(503, "Submission record unavailable") from None


@router.get("/operations/{operation_id}/terms/{snapshot_id}")
async def terms_detail(operation_id: UUID, snapshot_id: UUID, request: Request):
    from robothor.autonomy.terms_audit import TermsAudit

    scope = await require_personal_owner(request)
    try:
        result = await asyncio.to_thread(
            TermsAudit(AutonomyStore()).read, scope, str(operation_id), str(snapshot_id)
        )
        return _safe(result)
    except PermissionError:
        raise HTTPException(404, "Submission record not found") from None
    except Exception:
        raise HTTPException(503, "Submission record unavailable") from None


@router.delete("/operations/{operation_id}/terms")
async def forget_terms(operation_id: UUID, request: Request):
    """The owner's own delete for what the browser observed on their behalf.

    Until this existed the routes offered `DELETE /resources/{id}` and
    `DELETE /grants/{id}` and nothing else, while the observation archive kept
    the rendered review page — the owner's name, date of birth, address and
    the answers they typed — indefinitely. The audit fact survives; only the
    sealed page text goes. Already-erased is a success, not a 404.
    """
    from robothor.autonomy.terms_audit import TermsAudit

    scope = await require_personal_owner(request)
    try:
        erased = await asyncio.to_thread(
            TermsAudit(AutonomyStore()).forget, scope, str(operation_id)
        )
        return _safe({"operation_id": str(operation_id), "erased": erased})
    except PermissionError:
        raise HTTPException(404, "Submission record not found") from None
    except Exception:
        raise HTTPException(503, "Submission record unavailable") from None


@router.get("/operations/{operation_id}/payment")
async def payment_status(operation_id: UUID, request: Request):
    from robothor.autonomy.payment_journal import PaymentJournal

    scope = await require_personal_owner(request)
    try:
        result = await asyncio.to_thread(
            PaymentJournal(AutonomyStore()).read, scope, str(operation_id)
        )
        return _safe(result)
    except PermissionError:
        raise HTTPException(404, "Payment record not found") from None
    except Exception:
        raise HTTPException(503, "Payment record unavailable") from None


@router.post("/operations/{operation_id}/abandon")
async def abandon_operation(operation_id: UUID, request: Request):
    """The owner's way out of an operation nobody can resolve.

    An external verification that lapses leaves money reserved against a
    result no check can observe. Only the authenticated person may declare
    that attempt failed; nothing automatic takes this decision.
    """
    scope = await require_personal_owner(request)
    await _body(request, StrictModel)
    try:
        await asyncio.to_thread(AutonomyStore().abandon, scope, str(operation_id))
    except (PermissionError, ValueError):
        raise HTTPException(409, "This task is not waiting to be cleared") from None
    except Exception:
        raise HTTPException(503, "Personal automation storage is not ready") from None
    return _safe({"operation_id": str(operation_id), "state": "failed"})


@router.post("/handoffs/{handoff_id}/check")
async def check_external_handoff(handoff_id: UUID, request: Request):
    from robothor.autonomy.handoff_recovery import HandoffChecks
    from robothor.autonomy.handoff_worker import check_one
    from robothor.autonomy.handoffs import HandoffStore

    scope = await require_personal_owner(request)
    store = AutonomyStore()
    try:
        await asyncio.to_thread(HandoffStore(store).acknowledge, scope, str(handoff_id))
    except Exception:
        raise HTTPException(409, "External verification is unavailable or expired") from None

    task = asyncio.create_task(check_one(HandoffChecks(store), scope, str(handoff_id)))
    _resumes.add(task)
    task.add_done_callback(_resumes.discard)
    return _safe({"id": str(handoff_id), "state": "checking"})
