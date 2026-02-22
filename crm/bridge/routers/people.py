"""People & Companies CRUD routes."""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from robothor.crm.dal import (
    create_company,
    create_person,
    delete_company,
    delete_person,
    get_company,
    get_person,
    list_companies,
    list_people,
    merge_companies,
    merge_people,
    update_company,
    update_person,
)

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
):
    result = list_people(search, limit)
    return {"people": result}


@router.post("/people")
async def api_create_person(body: CreatePersonRequest):
    if not body.firstName:
        return JSONResponse({"error": "firstName required"}, status_code=400)

    person_id = create_person(body.firstName, body.lastName, body.email, body.phone)
    if person_id:
        return {"id": person_id, "firstName": body.firstName, "lastName": body.lastName}
    return JSONResponse({"error": "failed to create person (may be blocked)"}, status_code=500)


@router.get("/people/{person_id}")
async def api_get_person(person_id: str):
    result = get_person(person_id)
    if not result:
        return JSONResponse({"error": "person not found"}, status_code=404)
    return result


@router.patch("/people/{person_id}")
async def api_update_person(person_id: str, body: UpdatePersonRequest):
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

    result = update_person(person_id, **fields)
    if result:
        return {"success": True, "id": person_id}
    return JSONResponse({"error": "failed to update person"}, status_code=500)


@router.delete("/people/{person_id}")
async def api_delete_person(person_id: str):
    if delete_person(person_id):
        return {"success": True, "id": person_id}
    return JSONResponse({"error": "person not found"}, status_code=404)


@router.post("/people/merge")
async def api_merge_people(body: MergeRequest):
    keeper_id, loser_id = body.get_ids()
    if not keeper_id or not loser_id:
        return JSONResponse({"error": "primaryId and secondaryId required"}, status_code=400)
    result = merge_people(keeper_id, loser_id)
    if result:
        return {"success": True, "merged": result}
    return JSONResponse({"error": "merge failed"}, status_code=500)


# ─── Companies ───────────────────────────────────────────────────────────


@router.get("/companies")
async def api_list_companies(
    search: str | None = Query(None),
    limit: int = Query(50),
):
    result = list_companies(search, limit)
    return {"companies": result}


@router.get("/companies/{company_id}")
async def api_get_company(company_id: str):
    result = get_company(company_id)
    if not result:
        return JSONResponse({"error": "company not found"}, status_code=404)
    return result


@router.patch("/companies/{company_id}")
async def api_update_company(company_id: str, body: UpdateCompanyRequest):
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

    result = update_company(company_id, **fields)
    if result:
        return {"success": True, "id": company_id}
    return JSONResponse({"error": "failed to update company"}, status_code=500)


@router.delete("/companies/{company_id}")
async def api_delete_company(company_id: str):
    if delete_company(company_id):
        return {"success": True, "id": company_id}
    return JSONResponse({"error": "company not found"}, status_code=404)


@router.post("/companies/merge")
async def api_merge_companies(body: MergeRequest):
    keeper_id, loser_id = body.get_ids()
    if not keeper_id or not loser_id:
        return JSONResponse({"error": "primaryId and secondaryId required"}, status_code=400)
    result = merge_companies(keeper_id, loser_id)
    if result:
        return {"success": True, "merged": result}
    return JSONResponse({"error": "merge failed"}, status_code=500)
