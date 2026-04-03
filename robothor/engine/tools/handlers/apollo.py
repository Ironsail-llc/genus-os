"""Apollo.io contact enrichment & search tool handlers."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}

_APOLLO_BASE = "https://api.apollo.io/api/v1"


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def _get_api_key() -> str:
    return os.environ.get("APOLLO_API_KEY", "")


def _headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }


def _slim_person(p: dict[str, Any]) -> dict[str, Any]:
    """Extract only the fields that matter from an Apollo person record."""
    org = p.get("organization") or {}
    return {
        "id": p.get("id", ""),
        "first_name": p.get("first_name", ""),
        "last_name": p.get("last_name", ""),
        "title": p.get("title", ""),
        "headline": p.get("headline", ""),
        "email": p.get("email", ""),
        "phone_numbers": (p.get("phone_numbers") or [])[:3],
        "linkedin_url": p.get("linkedin_url", ""),
        "organization_name": org.get("name", "") or p.get("organization_name", ""),
        "city": p.get("city", ""),
        "state": p.get("state", ""),
        "country": p.get("country", ""),
    }


def _slim_company(c: dict[str, Any]) -> dict[str, Any]:
    """Extract only the fields that matter from an Apollo organization record."""
    return {
        "id": c.get("id", ""),
        "name": c.get("name", ""),
        "website_url": c.get("website_url", ""),
        "linkedin_url": c.get("linkedin_url", ""),
        "phone": c.get("phone", ""),
        "estimated_num_employees": c.get("estimated_num_employees"),
        "industry": c.get("industry", ""),
        "city": c.get("city", ""),
        "state": c.get("state", ""),
        "country": c.get("country", ""),
        "short_description": c.get("short_description", ""),
    }


@_handler("apollo_search_people")
async def _apollo_search_people(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Search Apollo.io for people (FREE — no credits consumed)."""
    api_key = _get_api_key()
    if not api_key:
        return {"error": "APOLLO_API_KEY not configured"}

    per_page = min(args.get("per_page", 10), 25)
    payload: dict[str, Any] = {"per_page": per_page, "page": 1}

    if v := args.get("q_person_name"):
        payload["q_person_name"] = v
    if v := args.get("q_organization_name"):
        payload["q_organization_name"] = v
    if v := args.get("person_titles"):
        payload["person_titles"] = v
    if v := args.get("person_locations"):
        payload["person_locations"] = v

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_APOLLO_BASE}/mixed_people/search",
                headers=_headers(api_key),
                json=payload,
            )
            if resp.status_code == 429:
                return {"error": "Apollo rate limit exceeded. Try again later."}
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"Apollo API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"Apollo request failed: {e}"}

    people = [_slim_person(p) for p in (data.get("people") or [])[:per_page]]
    pagination = data.get("pagination") or {}
    return {
        "people": people,
        "count": len(people),
        "total_available": pagination.get("total_entries", 0),
        "note": "Use apollo_enrich_person to get email/phone (costs credits).",
    }


@_handler("apollo_enrich_person")
async def _apollo_enrich_person(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Enrich a person via Apollo.io (COSTS CREDITS)."""
    api_key = _get_api_key()
    if not api_key:
        return {"error": "APOLLO_API_KEY not configured"}

    # At least one identifier required
    has_id = any(args.get(k) for k in ("email", "linkedin_url"))
    has_name = args.get("first_name") and args.get("last_name") and args.get("organization_name")
    if not has_id and not has_name:
        return {
            "error": "Provide email, linkedin_url, or (first_name + last_name + organization_name)."
        }

    params: dict[str, Any] = {}
    for key in ("first_name", "last_name", "email", "organization_name", "domain", "linkedin_url"):
        if v := args.get(key):
            params[key] = v
    params["reveal_personal_emails"] = args.get("reveal_personal_emails", False)
    params["reveal_phone_number"] = args.get("reveal_phone_number", False)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_APOLLO_BASE}/people/match",
                headers=_headers(api_key),
                json=params,
            )
            if resp.status_code == 429:
                return {"error": "Apollo rate limit exceeded. Try again later."}
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"Apollo API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"Apollo request failed: {e}"}

    person = data.get("person")
    if not person:
        return {"error": "No match found."}

    return {"person": _slim_person(person)}


@_handler("apollo_search_companies")
async def _apollo_search_companies(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Search Apollo.io for companies (COSTS CREDITS)."""
    api_key = _get_api_key()
    if not api_key:
        return {"error": "APOLLO_API_KEY not configured"}

    per_page = min(args.get("per_page", 10), 25)
    payload: dict[str, Any] = {"per_page": per_page, "page": 1}

    if v := args.get("q_organization_name"):
        payload["q_organization_name"] = v
    if v := args.get("organization_domains"):
        payload["organization_domains"] = v
    if v := args.get("organization_locations"):
        payload["organization_locations"] = v
    if v := args.get("organization_num_employees_ranges"):
        payload["organization_num_employees_ranges"] = v

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_APOLLO_BASE}/mixed_companies/search",
                headers=_headers(api_key),
                json=payload,
            )
            if resp.status_code == 429:
                return {"error": "Apollo rate limit exceeded. Try again later."}
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"Apollo API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"Apollo request failed: {e}"}

    orgs = [_slim_company(o) for o in (data.get("organizations") or [])[:per_page]]
    pagination = data.get("pagination") or {}
    return {
        "companies": orgs,
        "count": len(orgs),
        "total_available": pagination.get("total_entries", 0),
    }


@_handler("apollo_enrich_company")
async def _apollo_enrich_company(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Enrich a company by domain via Apollo.io (COSTS CREDITS)."""
    api_key = _get_api_key()
    if not api_key:
        return {"error": "APOLLO_API_KEY not configured"}

    domain = args.get("domain", "")
    if not domain:
        return {"error": "domain is required."}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{_APOLLO_BASE}/organizations/enrich",
                headers=_headers(api_key),
                params={"domain": domain},
            )
            if resp.status_code == 429:
                return {"error": "Apollo rate limit exceeded. Try again later."}
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"Apollo API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"Apollo request failed: {e}"}

    org = data.get("organization")
    if not org:
        return {"error": "No match found."}

    return {"company": _slim_company(org)}
