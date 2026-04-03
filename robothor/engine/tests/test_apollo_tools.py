"""Tests for Apollo.io contact enrichment & search tool handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from robothor.engine.tools.constants import APOLLO_TOOLS, READONLY_TOOLS
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.apollo import (
    HANDLERS,
    _slim_company,
    _slim_person,
)

# ─── Fixtures ─────────────────────────────────────────────────────

CTX = ToolContext(agent_id="test", tenant_id="robothor-primary")

SAMPLE_PERSON = {
    "id": "p-1",
    "first_name": "Jane",
    "last_name": "Doe",
    "title": "CTO",
    "headline": "CTO at Acme Inc",
    "email": "jane@acme.com",
    "phone_numbers": [{"raw_number": "+1555000111"}, {"raw_number": "+1555000222"}],
    "linkedin_url": "https://linkedin.com/in/janedoe",
    "organization_name": "Acme Inc",
    "organization": {"name": "Acme Inc"},
    "city": "New York",
    "state": "New York",
    "country": "United States",
    "employment_history": [{"title": "VP Eng"}, {"title": "Senior Dev"}],
    "seniority": "c_suite",
    "departments": ["engineering"],
}

SAMPLE_COMPANY = {
    "id": "o-1",
    "name": "Acme Inc",
    "website_url": "https://acme.com",
    "linkedin_url": "https://linkedin.com/company/acme",
    "phone": "+1555000000",
    "estimated_num_employees": 250,
    "industry": "Software",
    "city": "New York",
    "state": "New York",
    "country": "United States",
    "short_description": "A great company",
    "suborganizations": [{"name": "sub1"}],
    "departmental_head_count": {"engineering": 50},
}


# ─── Schema & Constants Tests ──────────────────────────────────────


class TestApolloConstants:
    def test_apollo_tools_has_four_members(self):
        assert len(APOLLO_TOOLS) == 4
        assert {
            "apollo_search_people",
            "apollo_enrich_person",
            "apollo_search_companies",
            "apollo_enrich_company",
        } == APOLLO_TOOLS

    def test_search_people_in_readonly(self):
        assert "apollo_search_people" in READONLY_TOOLS

    def test_credit_tools_not_readonly(self):
        for tool in ("apollo_enrich_person", "apollo_search_companies", "apollo_enrich_company"):
            assert tool not in READONLY_TOOLS

    def test_handlers_registered(self):
        for tool in APOLLO_TOOLS:
            assert tool in HANDLERS, f"{tool} not in HANDLERS"


# ─── Truncation Tests ──────────────────────────────────────────────


class TestTruncation:
    def test_slim_person_strips_nested(self):
        result = _slim_person(SAMPLE_PERSON)
        assert "employment_history" not in result
        assert "seniority" not in result
        assert "departments" not in result
        assert result["first_name"] == "Jane"
        assert result["organization_name"] == "Acme Inc"

    def test_slim_company_strips_nested(self):
        result = _slim_company(SAMPLE_COMPANY)
        assert "suborganizations" not in result
        assert "departmental_head_count" not in result
        assert result["name"] == "Acme Inc"
        assert result["estimated_num_employees"] == 250


# ─── Handler Tests ─────────────────────────────────────────────────


def _mock_response(json_data: dict, status_code: int = 200) -> httpx.Response:
    """Build a fake httpx.Response."""
    resp = httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("POST", "https://api.apollo.io/test"),
    )
    return resp


class TestApolloSearchPeople:
    @pytest.mark.asyncio
    async def test_missing_api_key(self):
        with patch("robothor.engine.tools.handlers.apollo._get_api_key", return_value=""):
            result = await HANDLERS["apollo_search_people"]({}, CTX)
        assert "error" in result
        assert "APOLLO_API_KEY" in result["error"]

    @pytest.mark.asyncio
    async def test_search_returns_trimmed(self):
        mock_resp = _mock_response({"people": [SAMPLE_PERSON], "pagination": {"total_entries": 42}})
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with (
            patch("robothor.engine.tools.handlers.apollo._get_api_key", return_value="test-key"),
            patch(
                "robothor.engine.tools.handlers.apollo.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            result = await HANDLERS["apollo_search_people"]({"q_person_name": "Jane Doe"}, CTX)

        assert result["count"] == 1
        assert result["total_available"] == 42
        assert result["people"][0]["first_name"] == "Jane"
        assert "employment_history" not in result["people"][0]
        assert "note" in result

    @pytest.mark.asyncio
    async def test_rate_limit_429(self):
        mock_resp = _mock_response({}, status_code=429)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with (
            patch("robothor.engine.tools.handlers.apollo._get_api_key", return_value="test-key"),
            patch(
                "robothor.engine.tools.handlers.apollo.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            result = await HANDLERS["apollo_search_people"]({}, CTX)

        assert "rate limit" in result["error"].lower()


class TestApolloEnrichPerson:
    @pytest.mark.asyncio
    async def test_missing_api_key(self):
        with patch("robothor.engine.tools.handlers.apollo._get_api_key", return_value=""):
            result = await HANDLERS["apollo_enrich_person"]({}, CTX)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_identifiers(self):
        with patch("robothor.engine.tools.handlers.apollo._get_api_key", return_value="test-key"):
            result = await HANDLERS["apollo_enrich_person"]({}, CTX)
        assert "error" in result
        assert "email" in result["error"].lower() or "linkedin" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_enrich_by_email(self):
        mock_resp = _mock_response({"person": SAMPLE_PERSON})
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with (
            patch("robothor.engine.tools.handlers.apollo._get_api_key", return_value="test-key"),
            patch(
                "robothor.engine.tools.handlers.apollo.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            result = await HANDLERS["apollo_enrich_person"]({"email": "jane@acme.com"}, CTX)

        assert "person" in result
        assert result["person"]["email"] == "jane@acme.com"
        assert "employment_history" not in result["person"]

    @pytest.mark.asyncio
    async def test_enrich_no_match(self):
        mock_resp = _mock_response({"person": None})
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with (
            patch("robothor.engine.tools.handlers.apollo._get_api_key", return_value="test-key"),
            patch(
                "robothor.engine.tools.handlers.apollo.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            result = await HANDLERS["apollo_enrich_person"]({"email": "nobody@nowhere.com"}, CTX)

        assert "error" in result
        assert "no match" in result["error"].lower()


class TestApolloSearchCompanies:
    @pytest.mark.asyncio
    async def test_missing_api_key(self):
        with patch("robothor.engine.tools.handlers.apollo._get_api_key", return_value=""):
            result = await HANDLERS["apollo_search_companies"]({}, CTX)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_search_returns_trimmed(self):
        mock_resp = _mock_response(
            {"organizations": [SAMPLE_COMPANY], "pagination": {"total_entries": 10}}
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with (
            patch("robothor.engine.tools.handlers.apollo._get_api_key", return_value="test-key"),
            patch(
                "robothor.engine.tools.handlers.apollo.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            result = await HANDLERS["apollo_search_companies"]({"q_organization_name": "Acme"}, CTX)

        assert result["count"] == 1
        assert result["companies"][0]["name"] == "Acme Inc"
        assert "suborganizations" not in result["companies"][0]


class TestApolloEnrichCompany:
    @pytest.mark.asyncio
    async def test_missing_api_key(self):
        with patch("robothor.engine.tools.handlers.apollo._get_api_key", return_value=""):
            result = await HANDLERS["apollo_enrich_company"]({}, CTX)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_domain_required(self):
        with patch("robothor.engine.tools.handlers.apollo._get_api_key", return_value="test-key"):
            result = await HANDLERS["apollo_enrich_company"]({}, CTX)
        assert "error" in result
        assert "domain" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_enrich_by_domain(self):
        mock_resp = _mock_response({"organization": SAMPLE_COMPANY})
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with (
            patch("robothor.engine.tools.handlers.apollo._get_api_key", return_value="test-key"),
            patch(
                "robothor.engine.tools.handlers.apollo.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            result = await HANDLERS["apollo_enrich_company"]({"domain": "acme.com"}, CTX)

        assert "company" in result
        assert result["company"]["name"] == "Acme Inc"
        assert "suborganizations" not in result["company"]

    @pytest.mark.asyncio
    async def test_enrich_no_match(self):
        mock_resp = _mock_response({"organization": None})
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with (
            patch("robothor.engine.tools.handlers.apollo._get_api_key", return_value="test-key"),
            patch(
                "robothor.engine.tools.handlers.apollo.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            result = await HANDLERS["apollo_enrich_company"]({"domain": "doesnotexist.xyz"}, CTX)

        assert "error" in result
        assert "no match" in result["error"].lower()
