"""Bridge routes for company hierarchy, outside ids and affiliations.

The DAL is mocked: these assert each route reaches the right DAL function with
the request's tenant, maps camelCase bodies to the DAL's arguments, and turns
the DAL's refusals (``ValueError``) into a 400 rather than a 500.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


PID = "11111111-2222-3333-4444-555555555555"
CID = "66666666-7777-8888-9999-000000000000"
AFF = {"id": "a1", "personId": PID, "companyId": CID, "role": "owner", "isActive": True}


@pytest.mark.asyncio
async def test_person_affiliations_lists_with_the_tenant(test_client):
    page = {"affiliations": [AFF], "total": 1, "offset": 0, "limit": 100, "hasMore": False}
    with patch("routers.people.list_affiliations_page", return_value=page) as mock:
        r = await test_client.get(
            f"/api/people/{PID}/affiliations?activeOnly=false",
            headers={"X-Tenant-Id": "tenant-b"},
        )
    assert r.status_code == 200
    assert r.json()["affiliations"] == [AFF]
    kw = mock.call_args.kwargs
    assert kw["person_id"] == PID
    assert kw["active_only"] is False
    assert kw["tenant_id"] == "tenant-b"


@pytest.mark.asyncio
async def test_set_affiliation_maps_the_body(test_client):
    out = {**AFF, "created": True, "changed": True}
    with patch("routers.people.set_affiliation", return_value=out) as mock:
        r = await test_client.post(
            f"/api/people/{PID}/affiliations",
            json={"companyId": CID, "role": "Owner", "attributes": {"k": 1}},
            headers={"X-Tenant-Id": "tenant-a"},
        )
    assert r.status_code == 200
    assert r.json() == out
    args, kw = mock.call_args
    assert args == (PID, CID, "Owner")
    assert kw == {"attributes": {"k": 1}, "is_active": True, "tenant_id": "tenant-a"}


@pytest.mark.asyncio
async def test_set_affiliation_refusal_is_a_400(test_client):
    with patch("routers.people.set_affiliation", side_effect=ValueError("company x not found")):
        r = await test_client.post(
            f"/api/people/{PID}/affiliations", json={"companyId": CID, "role": "owner"}
        )
    assert r.status_code == 400
    assert r.json() == {"error": "company x not found"}


@pytest.mark.asyncio
async def test_set_affiliation_requires_company_and_role(test_client):
    r = await test_client.post(f"/api/people/{PID}/affiliations", json={"role": "owner"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_close_affiliation(test_client):
    closed = {**AFF, "isActive": False, "until": "2026-11-17T00:00:00+00:00"}
    with patch("routers.people.close_affiliation", return_value=closed) as mock:
        r = await test_client.post(
            f"/api/people/{PID}/affiliations/close", json={"companyId": CID, "role": "owner"}
        )
    assert r.status_code == 200
    assert r.json() == closed
    assert mock.call_args.args == (PID, CID, "owner")
    with patch("routers.people.close_affiliation", return_value=None):
        r = await test_client.post(
            f"/api/people/{PID}/affiliations/close", json={"companyId": CID, "role": "owner"}
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_company_children(test_client):
    with patch("routers.people.list_company_children", return_value=[{"id": "c2"}]) as mock:
        r = await test_client.get(
            f"/api/companies/{CID}/children", headers={"X-Tenant-Id": "tenant-b"}
        )
    assert r.status_code == 200
    assert r.json() == {"children": [{"id": "c2"}]}
    assert mock.call_args.kwargs["tenant_id"] == "tenant-b"


@pytest.mark.asyncio
async def test_company_affiliations_filters_by_role(test_client):
    page = {"affiliations": [AFF], "total": 1, "offset": 0, "limit": 100, "hasMore": False}
    with patch("routers.people.list_affiliations_page", return_value=page) as mock:
        r = await test_client.get(f"/api/companies/{CID}/affiliations?role=owner")
    assert r.status_code == 200
    kw = mock.call_args.kwargs
    assert kw["company_id"] == CID and kw["role"] == "owner" and kw["active_only"] is True


@pytest.mark.asyncio
async def test_company_structure(test_client):
    view = {
        "company": {"id": CID},
        "parent": None,
        "parentChain": [],
        "children": [],
        "affiliations": [],
        "identifiers": [],
    }
    with patch("routers.people.get_company_view", return_value=view):
        r = await test_client.get(f"/api/companies/{CID}/structure")
    assert r.status_code == 200
    assert r.json() == view
    with patch("routers.people.get_company_view", return_value=None):
        r = await test_client.get(f"/api/companies/{CID}/structure")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_find_company_by_identifier_is_not_shadowed_by_the_id_route(test_client):
    with (
        patch("routers.people.get_company_by_identifier", return_value={"id": CID}) as mock,
        patch("routers.people.get_company") as by_id,
    ):
        r = await test_client.get(
            "/api/companies/by-identifier?system=test-system&identifier=x-1",
            headers={"X-Tenant-Id": "tenant-a"},
        )
    assert r.status_code == 200
    assert r.json() == {"id": CID}
    by_id.assert_not_called()
    assert mock.call_args.args == ("test-system", "x-1")
    assert mock.call_args.kwargs == {"tenant_id": "tenant-a"}
    with patch("routers.people.get_company_by_identifier", return_value=None):
        r = await test_client.get("/api/companies/by-identifier?system=s&identifier=missing")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_find_person_by_identifier(test_client):
    with (
        patch("routers.people.get_person_by_identifier", return_value={"id": PID}) as mock,
        patch("routers.people.get_person") as by_id,
    ):
        r = await test_client.get("/api/people/by-identifier?system=test-system&identifier=u-1")
    assert r.status_code == 200
    assert r.json() == {"id": PID}
    by_id.assert_not_called()
    assert mock.call_args.args == ("test-system", "u-1")


@pytest.mark.asyncio
async def test_existing_company_and_person_routes_are_unchanged(test_client):
    with patch("routers.people.get_company", return_value={"id": CID, "name": "Acme"}):
        r = await test_client.get(f"/api/companies/{CID}")
    assert r.status_code == 200 and r.json()["name"] == "Acme"
    with patch("routers.people.list_companies", return_value=[]) as mock:
        r = await test_client.get("/api/companies?search=ac")
    assert r.json() == {"companies": []}
    assert mock.call_args.args[0] == "ac"


@pytest.mark.asyncio
async def test_company_affiliations_page_with_a_total(test_client):
    page = {"affiliations": [AFF], "total": 600, "offset": 50, "limit": 1, "hasMore": True}
    with patch("routers.people.list_affiliations_page", return_value=page) as mock:
        r = await test_client.get(f"/api/companies/{CID}/affiliations?limit=1&offset=50")
    assert r.status_code == 200
    assert r.json() == page
    kw = mock.call_args.kwargs
    assert (kw["limit"], kw["offset"], kw["company_id"]) == (1, 50, CID)


@pytest.mark.asyncio
async def test_person_affiliations_page_with_a_total(test_client):
    page = {"affiliations": [AFF], "total": 1, "offset": 0, "limit": 100, "hasMore": False}
    with patch("routers.people.list_affiliations_page", return_value=page) as mock:
        r = await test_client.get(f"/api/people/{PID}/affiliations")
    assert r.json() == page
    assert mock.call_args.kwargs["person_id"] == PID
