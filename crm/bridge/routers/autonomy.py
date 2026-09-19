"""Personal onboarding, authority and operation status. Secrets travel inward only."""

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr

from robothor.autonomy.broker import ExecutionPlan
from robothor.autonomy.identity import scope_for_actor
from robothor.autonomy.models import Delegation, ResourceInput, RuntimeSettings, StrictModel
from robothor.autonomy.onboarding import import_contact_profile
from robothor.autonomy.runtime import run_browser
from robothor.autonomy.store import AutonomyStore

router = APIRouter(prefix="/api/autonomy", tags=["autonomy"])
_resumes: set[asyncio.Task] = set()


class VerificationInput(StrictModel):
    verification_code: SecretStr


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


async def _body(request: Request, model):
    # Do not use automatic FastAPI model parsing here: validation responses
    # otherwise echo rejected input (including a pasted card or password).
    try:
        raw = await request.body()
        if len(raw) > 8_100_000:
            raise ValueError
        return model.model_validate(json.loads(raw))
    except Exception:
        raise HTTPException(422, "Invalid enrollment data; no values were stored") from None


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
    policy = await _body(request, Delegation)
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
    return _safe({"operations": rows})


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
        plan = ExecutionPlan.model_validate(row["execution_plan"])
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
