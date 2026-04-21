"""People & Companies CRUD routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from deps import get_tenant_id
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from robothor.crm.dal import (
    create_person,
    delete_company,
    delete_person,
    get_company,
    get_contact_360,
    get_person,
    get_person_calls,
    get_person_events,
    get_person_memory,
    get_person_messages,
    get_person_notes,
    get_person_runs,
    get_person_summary,
    get_person_tasks,
    get_person_timeline,
    list_companies,
    list_people,
    merge_companies,
    merge_people,
    update_company,
    update_person,
)

if TYPE_CHECKING:
    from models import (
        CreatePersonRequest,
        MergeRequest,
        UpdateCompanyRequest,
        UpdatePersonRequest,
    )

router = APIRouter(prefix="/api", tags=["people", "companies"])


# ─── People ──────────────────────────────────────────────────────────────


@router.get("/people")
async def api_list_people(
    search: str | None = Query(None),
    limit: int = Query(20),
    tenant_id: str = Depends(get_tenant_id),
):
    result = list_people(search, limit, tenant_id=tenant_id)
    return {"people": result}


@router.post("/people")
async def api_create_person(
    body: CreatePersonRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    if not body.firstName:
        return JSONResponse({"error": "firstName required"}, status_code=400)

    person_id = create_person(
        body.firstName, body.lastName, body.email, body.phone, tenant_id=tenant_id
    )
    if person_id:
        return {"id": person_id, "firstName": body.firstName, "lastName": body.lastName}
    return JSONResponse({"error": "failed to create person (may be blocked)"}, status_code=500)


@router.get("/people/{person_id}")
async def api_get_person(
    person_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    result = get_person(person_id, tenant_id=tenant_id)
    if not result:
        return JSONResponse({"error": "person not found"}, status_code=404)
    return result


@router.patch("/people/{person_id}")
async def api_update_person(
    person_id: str,
    body: UpdatePersonRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    fields = {}
    if body.jobTitle is not None:
        fields["job_title"] = body.jobTitle
    if body.companyId is not None:
        fields["company_id"] = body.companyId
    if body.city is not None:
        fields["city"] = body.city
    if body.linkedinUrl is not None:
        fields["linkedin_url"] = body.linkedinUrl
    if body.phone is not None:
        fields["phone"] = body.phone
    if body.avatarUrl is not None:
        fields["avatar_url"] = body.avatarUrl
    if body.firstName is not None:
        fields["first_name"] = body.firstName
    if body.lastName is not None:
        fields["last_name"] = body.lastName
    if body.email is not None:
        fields["email"] = body.email

    result = update_person(person_id, tenant_id=tenant_id, **fields)
    if result:
        return {"success": True, "id": person_id}
    return JSONResponse({"error": "failed to update person"}, status_code=500)


# ── Contact 360 read endpoints ───────────────────────────────────────────
# Person-scoped views over the unified data fabric. Each endpoint is a thin
# delegator to the DAL; the DAL owns the SQL.


@router.get("/people/{person_id}/timeline")
async def api_person_timeline(
    person_id: str,
    limit: int = Query(50, ge=1, le=500),
    channels: list[str] | None = Query(None),
    tenant_id: str = Depends(get_tenant_id),
):
    rows = get_person_timeline(person_id, limit=limit, channels=channels, tenant_id=tenant_id)
    return {"activities": rows}


@router.get("/people/{person_id}/summary")
async def api_person_summary(
    person_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    return get_person_summary(person_id, tenant_id=tenant_id)


@router.get("/people/{person_id}/messages")
async def api_person_messages(
    person_id: str,
    channel: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    tenant_id: str = Depends(get_tenant_id),
):
    rows = get_person_messages(person_id, channel=channel, limit=limit, tenant_id=tenant_id)
    return {"messages": rows}


@router.get("/people/{person_id}/calls")
async def api_person_calls(
    person_id: str,
    limit: int = Query(100, ge=1, le=500),
    tenant_id: str = Depends(get_tenant_id),
):
    return {"calls": get_person_calls(person_id, limit=limit, tenant_id=tenant_id)}


@router.get("/people/{person_id}/events")
async def api_person_events(
    person_id: str,
    limit: int = Query(100, ge=1, le=500),
    tenant_id: str = Depends(get_tenant_id),
):
    return {"events": get_person_events(person_id, limit=limit, tenant_id=tenant_id)}


@router.get("/people/{person_id}/tasks")
async def api_person_tasks(
    person_id: str,
    limit: int = Query(100, ge=1, le=500),
    tenant_id: str = Depends(get_tenant_id),
):
    return {"tasks": get_person_tasks(person_id, limit=limit, tenant_id=tenant_id)}


@router.get("/people/{person_id}/notes")
async def api_person_notes(
    person_id: str,
    limit: int = Query(100, ge=1, le=500),
    tenant_id: str = Depends(get_tenant_id),
):
    return {"notes": get_person_notes(person_id, limit=limit, tenant_id=tenant_id)}


@router.get("/people/{person_id}/runs")
async def api_person_runs(
    person_id: str,
    limit: int = Query(100, ge=1, le=500),
    tenant_id: str = Depends(get_tenant_id),
):
    return {"runs": get_person_runs(person_id, limit=limit, tenant_id=tenant_id)}


@router.get("/people/{person_id}/memory")
async def api_person_memory(
    person_id: str,
    limit: int = Query(50, ge=1, le=500),
    tenant_id: str = Depends(get_tenant_id),
):
    return {"memory": get_person_memory(person_id, limit=limit, tenant_id=tenant_id)}


@router.get("/people/{person_id}/contact-360")
async def api_person_contact_360(
    person_id: str,
    timeline_limit: int = Query(50, ge=1, le=500),
    tenant_id: str = Depends(get_tenant_id),
):
    result = get_contact_360(person_id, tenant_id=tenant_id, timeline_limit=timeline_limit)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return result


@router.delete("/people/{person_id}")
async def api_delete_person(
    person_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    if delete_person(person_id, tenant_id=tenant_id):
        return {"success": True, "id": person_id}
    return JSONResponse({"error": "person not found"}, status_code=404)


@router.post("/people/merge")
async def api_merge_people(
    body: MergeRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    keeper_id, loser_id = body.get_ids()
    if not keeper_id or not loser_id:
        return JSONResponse({"error": "primaryId and secondaryId required"}, status_code=400)
    result = merge_people(keeper_id, loser_id, tenant_id=tenant_id)
    if result:
        return {"success": True, "merged": result}
    return JSONResponse({"error": "merge failed"}, status_code=500)


# ─── Companies ───────────────────────────────────────────────────────────


@router.get("/companies")
async def api_list_companies(
    search: str | None = Query(None),
    limit: int = Query(50),
    tenant_id: str = Depends(get_tenant_id),
):
    result = list_companies(search, limit, tenant_id=tenant_id)
    return {"companies": result}


@router.get("/companies/{company_id}")
async def api_get_company(
    company_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    result = get_company(company_id, tenant_id=tenant_id)
    if not result:
        return JSONResponse({"error": "company not found"}, status_code=404)
    return result


@router.patch("/companies/{company_id}")
async def api_update_company(
    company_id: str,
    body: UpdateCompanyRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    fields = {}
    if body.domainName is not None:
        fields["domain_name"] = body.domainName
    if body.employees is not None:
        fields["employees"] = body.employees
    if body.address is not None:
        fields["address"] = body.address
    if body.linkedinUrl is not None:
        fields["linkedin_url"] = body.linkedinUrl
    if body.idealCustomerProfile is not None:
        fields["ideal_customer_profile"] = body.idealCustomerProfile
    if body.name is not None:
        fields["name"] = body.name

    result = update_company(company_id, tenant_id=tenant_id, **fields)
    if result:
        return {"success": True, "id": company_id}
    return JSONResponse({"error": "failed to update company"}, status_code=500)


@router.delete("/companies/{company_id}")
async def api_delete_company(
    company_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    if delete_company(company_id, tenant_id=tenant_id):
        return {"success": True, "id": company_id}
    return JSONResponse({"error": "company not found"}, status_code=404)


@router.post("/companies/merge")
async def api_merge_companies(
    body: MergeRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    keeper_id, loser_id = body.get_ids()
    if not keeper_id or not loser_id:
        return JSONResponse({"error": "primaryId and secondaryId required"}, status_code=400)
    result = merge_companies(keeper_id, loser_id, tenant_id=tenant_id)
    if result:
        return {"success": True, "merged": result}
    return JSONResponse({"error": "merge failed"}, status_code=500)
